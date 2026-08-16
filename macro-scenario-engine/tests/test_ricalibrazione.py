"""Test della ricalibrazione misurata sulla rigiocata 2007-2026.

I quattro difetti misurati (20.901 scenari valutati) e le rispettive correzioni:
  1. banda neutra fissa ±0.15% -> banda adattiva su volatilita' e orizzonte;
  2. soglia unica a 15 -> soglia di direzione separata e piu' bassa;
  3. probabilita' dichiarate ~45% contro frequenze reali ~21% -> compressione
     verso 1/3 a somma invariata;
  4. confidenza invertita (ALTA peggio di BASSA) -> un LATERALE senza segnale
     non supera mai BASSA.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.calibration import evaluation
from app.config import get_config
from app.engine import scenarios as scenario_engine
from app.engine import scoring
from app.storage import database
from app.storage.models import PriceBar, Scenario, ScenarioOutcome
from app.storage.repositories import now_utc
from conftest import make_event, set_rate


# --------------------------------------------------------------------------- #
#  1. Banda neutra adattiva
# --------------------------------------------------------------------------- #


def _alternating_bars(session, pair: str, start: datetime, count: int, step_pct: float = 1.0):
    """Barre giornaliere con rendimenti alternati ±step_pct: volatilita' nota."""
    close = 1.0
    closes = []
    giorno = start
    for i in range(count):
        session.add(PriceBar(pair=pair, timestamp_utc=giorno, close=close, source="test"))
        closes.append(close)
        close *= 1.0 + (step_pct / 100.0 if i % 2 == 0 else -step_pct / 100.0)
        giorno += timedelta(days=1)
    session.flush()
    return closes


def test_sigma_trascinata_corrisponde_alla_deviazione_standard(session):
    start = datetime(2020, 1, 1)
    closes = _alternating_bars(session, "EURUSD", start, 40)
    moment = start + timedelta(days=39)

    sigma = evaluation.trailing_sigma_pct(session, "EURUSD", moment)
    returns = [(b - a) / a * 100.0 for a, b in zip(closes, closes[1:])]
    assert sigma == pytest.approx(statistics.stdev(returns), rel=1e-9)


def test_sigma_non_guarda_nel_futuro(session):
    """Barre successive al momento di generazione non toccano la soglia."""
    start = datetime(2020, 1, 1)
    _alternating_bars(session, "EURUSD", start, 40, step_pct=0.5)
    moment = start + timedelta(days=39)
    sigma_prima = evaluation.trailing_sigma_pct(session, "EURUSD", moment)

    # Un crollo selvaggio DOPO il momento simulato...
    session.add(PriceBar(pair="EURUSD", timestamp_utc=moment + timedelta(days=1),
                         close=0.5, source="test"))
    session.flush()
    # ...non deve cambiare la volatilita' vista "a quella data".
    assert evaluation.trailing_sigma_pct(session, "EURUSD", moment) == sigma_prima


def test_sigma_none_con_poche_barre(session):
    _alternating_bars(session, "EURUSD", datetime(2020, 1, 1), evaluation.VOL_MIN_BARS - 1)
    assert evaluation.trailing_sigma_pct(session, "EURUSD", datetime(2020, 3, 1)) is None


def test_banda_scala_con_volatilita_e_orizzonte():
    banda_3g, metodo = evaluation.neutral_band_for(1.0, 72)
    assert metodo == "vol_scalata"
    assert banda_3g == pytest.approx(evaluation.EQUIPROBABLE_Z * 1.0 * (3.0 ** 0.5), rel=1e-9)
    # Stesso orizzonte, volatilita' doppia -> banda doppia.
    banda_doppia, _ = evaluation.neutral_band_for(2.0, 72)
    assert banda_doppia == pytest.approx(banda_3g * 2.0, rel=1e-9)
    # Stessa volatilita', orizzonte 4 volte piu' lungo -> banda doppia (radice).
    banda_12g, _ = evaluation.neutral_band_for(1.0, 288)
    assert banda_12g == pytest.approx(banda_3g * 2.0, rel=1e-9)


def test_banda_ha_pavimento_e_riserva():
    # Volatilita' quasi nulla: la banda non scende sotto il pavimento.
    banda, metodo = evaluation.neutral_band_for(0.001, 24)
    assert metodo == "vol_scalata"
    assert banda == evaluation.NEUTRAL_BAND_MIN_PCT
    # Senza volatilita' stimabile: banda fissa storica, dichiarata come riserva.
    banda, metodo = evaluation.neutral_band_for(None, 72)
    assert (banda, metodo) == (evaluation.NEUTRAL_BAND_PCT, "fissa_fallback")


def test_classify_move_usa_la_banda_passata():
    assert evaluation.classify_move(0.5, band_pct=0.75) == "LATERALE"
    assert evaluation.classify_move(0.5, band_pct=0.15) == "RIALZO"
    assert evaluation.classify_move(-0.5, band_pct=0.15) == "RIBASSO"


def test_valutazione_usa_banda_adattiva_e_la_registra(session):
    """+0.3% in 3 giorni su una coppia con vol ~1%/giorno: LATERALE, non RIALZO.

    Con la vecchia banda fissa ±0.15% lo stesso movimento era 'RIALZO': era
    esattamente il difetto misurato (80% di esiti direzionali su orizzonti
    multi-giorno).
    """
    start = datetime(2024, 1, 1)
    closes = _alternating_bars(session, "EURUSD", start, 60)  # vol ~1%/giorno
    generated = start + timedelta(days=59)
    ultimo = closes[-1]

    # Fine orizzonte 3 giorni dopo, con chiusura a +0.3% dal via.
    horizon_end = generated + timedelta(hours=72)
    session.add(PriceBar(pair="EURUSD", timestamp_utc=horizon_end,
                         close=ultimo * 1.003, source="test"))
    session.add(Scenario(
        pair="EURUSD", horizon="h24_72",
        generated_at=generated, expires_at=generated + timedelta(hours=12),
        horizon_end=horizon_end,
        bias=0.0, bias_label="NEUTRAL", confidence="BASSA", confidence_score=10.0,
        base_direction="LATERALE", base_probability=0.35,
        alt_a_probability=0.33, alt_b_probability=0.32,
        payload={}, rewritable=False,
    ))
    session.flush()

    esito = evaluation.evaluate_due_scenarios(session)
    assert esito["valutati"] == 1

    outcome = session.query(ScenarioOutcome).one()
    sigma = evaluation.trailing_sigma_pct(session, "EURUSD", generated)
    banda_attesa, _ = evaluation.neutral_band_for(sigma, 72)
    assert outcome.neutral_band_pct == pytest.approx(banda_attesa, rel=1e-6)
    assert banda_attesa > 0.3  # il movimento resta dentro la banda
    assert outcome.realized_direction == "LATERALE"
    assert outcome.base_hit is True


def test_report_affidabilita_dichiara_la_banda(session):
    test_valutazione_usa_banda_adattiva_e_la_registra(session)
    metrics = evaluation.reliability_metrics(session)
    banda = metrics["banda_neutra"]
    assert "adattiva" in banda["metodo"]
    assert banda["media_pct"] is not None and banda["media_pct"] > 0.3
    assert banda["esiti_valutati_con_banda_fissa_storica"] == 0
    assert "lookahead" in metrics["nota_metodo"]


# --------------------------------------------------------------------------- #
#  2. Soglia di direzione separata
# --------------------------------------------------------------------------- #


def test_soglia_direzione_separata_e_piu_bassa():
    soglie = get_config().weights["soglie_bias"]
    assert float(soglie["direzione"]) < float(soglie["normale"])
    # Un bias sopra la soglia di direzione ma sotto quella dell'etichetta
    # produce una chiamata direzionale con etichetta ancora NEUTRAL.
    bias = (float(soglie["direzione"]) + float(soglie["normale"])) / 2.0
    assert scenario_engine._direction_from_bias(bias, float(soglie["direzione"])) == "RIALZO"
    assert scenario_engine.classify_bias(bias) == "NEUTRAL"


# --------------------------------------------------------------------------- #
#  3. Compressione delle probabilita'
# --------------------------------------------------------------------------- #


def test_probabilita_compresse_verso_un_terzo():
    cfg = get_config().weights["scenari"]
    lam = float(cfg["shrinkage_lambda"])
    assert 0.0 <= lam < 1.0  # la compressione deve essere attiva

    ctx = SimpleNamespace(bias=80.0, event_risk=0.0)
    p = scenario_engine._probabilities(ctx, 90.0, 1, -1)
    assert sum(p) == pytest.approx(1.0, abs=1e-9)

    # Il massimo dichiarabile scende da prob_base_max al suo valore compresso.
    massimo_compresso = 1.0 / 3.0 + lam * (float(cfg["prob_base_max"]) - 1.0 / 3.0)
    assert p[0] <= massimo_compresso + 1e-9
    # Ma il segnale conta ancora: con bias forte il base resta sopra 1/3.
    assert p[0] > 1.0 / 3.0
    # E nessuna alternativa scende sotto il minimo compresso.
    minimo_compresso = 1.0 / 3.0 + lam * (float(cfg["prob_alt_min"]) - 1.0 / 3.0)
    assert min(p[1:]) >= minimo_compresso - 1e-9


def test_payload_dichiara_i_parametri_di_calibrazione(session):
    set_rate(session, "USD", 5.5)
    make_event(session, title="Core CPI y/y", currency="USD",
               actual=3.9, forecast=3.2, previous=3.2, hours_ago=6)
    evaluations = scoring.evaluate_all(session)
    payload = scenario_engine.generate_pair_scenarios(session, "EURUSD", evaluations)
    calib = payload["calibrazione"]
    soglie = get_config().weights["soglie_bias"]
    assert calib["soglia_direzione"] == float(soglie["direzione"])
    assert calib["shrinkage_lambda"] == float(
        get_config().weights["scenari"]["shrinkage_lambda"]
    )
    assert "rigiocata" in calib["nota"]


# --------------------------------------------------------------------------- #
#  4. Confidenza mai ALTA su un laterale senza segnale
# --------------------------------------------------------------------------- #


def test_laterale_senza_segnale_ha_confidenza_bassa(session):
    # Tassi tutti uguali e nessun evento: bias ~0 -> direzione LATERALE.
    for ccy in ("USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"):
        set_rate(session, ccy, 2.0)
    evaluations = scoring.evaluate_all(session)
    payload = scenario_engine.generate_pair_scenarios(session, "EURUSD", evaluations)

    assert payload["scenari"][0]["direzione"] == "LATERALE"
    assert payload["confidenza"] == "BASSA"
    assert any("LATERALE" in m for m in payload["confidenza_motivazioni"])
    # E il suggerimento resta "stare fuori", non "solo post evento".
    assert payload["scenari"][0]["suggerimento"]["azione"] == "STARE_FUORI"


# --------------------------------------------------------------------------- #
#  Strumento di misura: hit per fascia di bias e confidenza per classe
# --------------------------------------------------------------------------- #


def _scenario_valutato(session, *, bias, direction, confidence, hit, prob=0.4):
    """Scenario gia' valutato, costruito direttamente per testare le metriche."""
    g = datetime(2024, 1, 1)
    sc = Scenario(
        pair="EURUSD", horizon="h24_72", generated_at=g,
        expires_at=g + timedelta(hours=12), horizon_end=g + timedelta(hours=72),
        bias=bias, bias_label="NEUTRAL", confidence=confidence,
        confidence_score=50.0, base_direction=direction, base_probability=prob,
        alt_a_probability=0.3, alt_b_probability=0.3, payload={}, rewritable=False,
    )
    session.add(sc)
    session.flush()
    realized = direction if hit else ("RIBASSO" if direction != "RIBASSO" else "RIALZO")
    session.add(ScenarioOutcome(
        scenario_id=sc.id, evaluated_at=g + timedelta(hours=80),
        price_start=1.0, price_end=1.01, change_pct=1.0,
        realized_direction=realized, base_hit=hit,
        brier=(prob - (1.0 if hit else 0.0)) ** 2, neutral_band_pct=0.4,
    ))
    session.flush()


def test_hit_per_fascia_di_bias(session):
    # Fascia 8-12: due direzionali, uno centrato. Fascia 0-4: un laterale centrato.
    _scenario_valutato(session, bias=10.0, direction="RIALZO", confidence="MEDIA", hit=True)
    _scenario_valutato(session, bias=-11.0, direction="RIBASSO", confidence="MEDIA", hit=False)
    _scenario_valutato(session, bias=1.0, direction="LATERALE", confidence="BASSA", hit=True)

    metrics = evaluation.reliability_metrics(session)
    per_bias = {b["fascia_bias"]: b for b in metrics["per_bias"]}

    assert per_bias["8-12"]["campione"] == 2
    assert per_bias["8-12"]["direzionali"] == 2
    assert per_bias["8-12"]["hit_rate_direzionale"] == 50.0
    assert per_bias["0-4"]["campione"] == 1
    assert per_bias["0-4"]["direzionali"] == 0
    assert per_bias["0-4"]["hit_rate_direzionale"] is None
    # Le fasce vuote esistono comunque, dichiarate a campione zero.
    assert per_bias["24+"]["campione"] == 0
    assert "soglia" in metrics["nota_bias"]


def test_confidenza_separata_per_classe_prevista(session):
    # Stesso livello (BASSA) su classi diverse: laterale che centra,
    # direzionale che sbaglia. La marginale li mescola, lo split no.
    _scenario_valutato(session, bias=1.0, direction="LATERALE", confidence="BASSA", hit=True)
    _scenario_valutato(session, bias=9.0, direction="RIALZO", confidence="BASSA", hit=False)
    _scenario_valutato(session, bias=20.0, direction="RIBASSO", confidence="ALTA", hit=True)

    metrics = evaluation.reliability_metrics(session)

    assert metrics["per_confidenza"]["BASSA"]["campione"] == 2
    assert metrics["per_confidenza"]["BASSA"]["hit_rate"] == 50.0
    assert metrics["per_confidenza_laterale"]["BASSA"] == {
        "campione": 1, "hit_rate": 100.0,
        "brier": metrics["per_confidenza_laterale"]["BASSA"]["brier"],
    }
    assert metrics["per_confidenza_direzionale"]["BASSA"]["hit_rate"] == 0.0
    assert metrics["per_confidenza_direzionale"]["ALTA"]["hit_rate"] == 100.0
    assert "LATERALE" not in metrics["per_confidenza_direzionale"].get("MEDIA", {})
    assert metrics["nota_confidenza"]


# --------------------------------------------------------------------------- #
#  Migrazione dello schema sugli archivi esistenti
# --------------------------------------------------------------------------- #


def test_migrazione_aggiunge_le_colonne_mancanti(session):
    """Un archivio creato prima della colonna neutral_band_pct viene aggiornato
    in place da init_db, senza ricreare le tabelle ne' toccare i dati."""
    from sqlalchemy import inspect, text

    session.close()
    engine = database.get_engine()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE scenario_outcomes DROP COLUMN neutral_band_pct"))
    assert "neutral_band_pct" not in {
        c["name"] for c in inspect(engine).get_columns("scenario_outcomes")
    }

    database._migrate_schema(engine)
    assert "neutral_band_pct" in {
        c["name"] for c in inspect(engine).get_columns("scenario_outcomes")
    }
