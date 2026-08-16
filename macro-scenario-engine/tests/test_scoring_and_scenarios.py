"""Test sullo scoring valutario (3.2) e sul generatore di scenari (3.5)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import get_config
from app.engine import scenarios as scenario_engine
from app.engine import scoring
from app.engine.surprise import apply_and_store
from app.storage.models import Event
from app.storage.repositories import now_utc
from conftest import make_event, set_rate


def _score_events(session) -> None:
    for event in session.query(Event).all():
        if event.actual is not None:
            apply_and_store(session, event)
    session.flush()


def _build_usd_strong(session) -> None:
    set_rate(session, "USD", 5.5)
    for ccy in ("EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"):
        set_rate(session, ccy, 1.0)
    make_event(
        session,
        title="Non-Farm Employment Change",
        currency="USD",
        actual=350.0,
        forecast=180.0,
        previous=175.0,
        hours_ago=6,
    )
    make_event(
        session,
        title="Core CPI y/y",
        currency="USD",
        actual=3.9,
        forecast=3.2,
        previous=3.2,
        hours_ago=30,
    )
    _score_events(session)


def test_scores_stay_in_range(session):
    _build_usd_strong(session)
    evaluations = scoring.evaluate_all(session)
    for evaluation in evaluations.values():
        assert -100.0 <= evaluation.composite <= 100.0
        for sub in evaluation.subscores.values():
            assert -100.0 <= sub.value <= 100.0


def test_every_subscore_has_a_rationale(session):
    _build_usd_strong(session)
    evaluation = scoring.evaluate_currency(session, "USD")
    for name, sub in evaluation.subscores.items():
        assert sub.rationale, f"sotto-score {name} senza motivazione"
        assert len(sub.rationale.splitlines()) <= 2


def test_missing_data_lowers_quality_not_score(session):
    """Senza dati lo score resta neutro e la qualita' crolla: nessuna finzione."""
    evaluation = scoring.evaluate_currency(session, "NZD")
    assert abs(evaluation.composite) < 40
    assert evaluation.data_quality < 0.8
    assert any("senza dati" in note for note in evaluation.notes)


def test_synthetic_currency_is_inverse_of_reference(session):
    _build_usd_strong(session)
    evaluations = scoring.evaluate_all(session)
    usd = evaluations["USD"]
    xau = evaluations["XAU"]
    assert xau.synthetic is True
    assert xau.composite == pytest.approx(-0.6 * usd.composite, abs=0.1)
    assert xau.notes  # il limite del modello e' dichiarato


def test_regime_inverts_inflation_contribution(session, monkeypatch):
    make_event(
        session,
        title="Core CPI y/y",
        currency="EUR",
        actual=4.5,
        forecast=3.0,
        previous=3.0,
        hours_ago=12,
    )
    _score_events(session)

    hawkish = scoring.score_inflation_regime(session, "EUR", "HAWKISH_DOMINANT")
    credibility = scoring.score_inflation_regime(session, "EUR", "CREDIBILITY_LOSS")
    assert hawkish.value > 0
    assert credibility.value < 0
    assert credibility.chains[0].chain_id == "INFLAZIONE_CREDIBILITA"


def test_pair_bias_is_difference_of_scores(session):
    _build_usd_strong(session)
    evaluations = scoring.evaluate_all(session)
    ctx = scenario_engine.build_context(session, "EURUSD", evaluations)
    expected = (evaluations["EUR"].composite - evaluations["USD"].composite) / 2.0
    assert ctx.bias == pytest.approx(max(-100.0, min(100.0, expected)), abs=0.01)
    assert ctx.bias < 0  # USD forte => EURUSD ribassista


def test_three_scenarios_sum_to_100(session):
    _build_usd_strong(session)
    evaluations = scoring.evaluate_all(session)
    for pair in get_config().pairs:
        payload = scenario_engine.generate_pair_scenarios(session, pair, evaluations)
        probabilities = [s["probabilita"] for s in payload["scenari"]]
        assert len(probabilities) == 3
        assert sum(probabilities) == pytest.approx(100.0, abs=0.2)


def test_base_probability_is_capped(session):
    _build_usd_strong(session)
    evaluations = scoring.evaluate_all(session)
    cap = get_config().weights["scenari"]["prob_base_max"] * 100
    payload = scenario_engine.generate_pair_scenarios(session, "EURUSD", evaluations)
    assert payload["scenari"][0]["probabilita"] <= cap + 0.05
    for scenario in payload["scenari"][1:]:
        assert scenario["probabilita"] >= get_config().weights["scenari"]["prob_alt_min"] * 100 - 0.05


def test_scenario_payload_is_complete(session):
    _build_usd_strong(session)
    evaluations = scoring.evaluate_all(session)
    payload = scenario_engine.generate_pair_scenarios(session, "EURUSD", evaluations)

    assert payload["confidenza"] in ("ALTA", "MEDIA", "BASSA")
    assert payload["scade_il"] > payload["generato_il"]
    assert payload["disclaimer"]

    for scenario in payload["scenari"]:
        assert scenario["direzione"] in ("RIALZO", "RIBASSO", "LATERALE")
        assert scenario["magnitudo"] in ("contenuta", "moderata", "ampia")
        assert scenario["invalidazione"], "ogni scenario deve avere condizioni di invalidazione"
        assert scenario["catene_causali"], "ogni scenario deve dichiarare le catene attive"
        assert scenario["suggerimento"]["azione"]
        assert scenario["confidenza"] in ("ALTA", "MEDIA", "BASSA")


def test_imminent_red_event_downgrades_confidence(session):
    _build_usd_strong(session)
    evaluations = scoring.evaluate_all(session)
    before = scenario_engine.generate_pair_scenarios(session, "EURUSD", evaluations)

    make_event(
        session,
        title="FOMC Statement",
        currency="USD",
        impact="RED",
        hours_ago=-3,  # fra 3 ore
    )
    session.flush()
    after = scenario_engine.generate_pair_scenarios(session, "EURUSD", evaluations)

    order = {"BASSA": 0, "MEDIA": 1, "ALTA": 2}
    assert after["riscrivibile"] is True
    assert order[after["confidenza"]] <= order[before["confidenza"]]
    assert any("riscrivibile" in m for m in after["confidenza_motivazioni"])


def test_no_trade_window_suggests_staying_out(session):
    _build_usd_strong(session)
    evaluations = scoring.evaluate_all(session)
    make_event(
        session,
        title="FOMC Statement",
        currency="USD",
        impact="RED",
        hours_ago=-0.25,  # fra 15 minuti: dentro la finestra di blocco
    )
    session.flush()
    payload = scenario_engine.generate_pair_scenarios(session, "EURUSD", evaluations)
    assert payload["scenari"][0]["suggerimento"]["azione"] == "STARE_FUORI"


def test_scenarios_are_persisted_for_calibration(session):
    _build_usd_strong(session)
    evaluations = scoring.evaluate_all(session)
    scenario_engine.generate_all(session, evaluations, persist=True)
    session.flush()

    from app.storage.models import Scenario

    rows = session.query(Scenario).all()
    assert len(rows) == len(get_config().pairs)
    for row in rows:
        assert row.horizon_end > row.generated_at
        assert 0.0 < row.base_probability <= 0.65
        assert row.payload["scenari"]


def test_horizon_changes_validity_window(session):
    _build_usd_strong(session)
    evaluations = scoring.evaluate_all(session)
    intraday = scenario_engine.generate_pair_scenarios(
        session, "EURUSD", evaluations, horizon="intraday"
    )
    weekly = scenario_engine.generate_pair_scenarios(
        session, "EURUSD", evaluations, horizon="w1_4"
    )
    assert intraday["fine_orizzonte"] < weekly["fine_orizzonte"]
