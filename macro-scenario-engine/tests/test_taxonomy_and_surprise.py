"""Test su normalizzazione dei valori, categorie e classificazione delle sorprese."""

from __future__ import annotations

import pytest

from app.engine import surprise, taxonomy
from conftest import make_event


@pytest.mark.parametrize(
    "raw,expected_value,expected_unit",
    [
        ("236K", 236.0, "K"),
        ("3.2%", 3.2, "%"),
        ("-1.5B", -1.5, "B"),
        ("<0.1%", 0.1, "%"),
        ("", None, None),
        ("--", None, None),
        (None, None, None),
    ],
)
def test_parse_value(raw, expected_value, expected_unit):
    value, unit = taxonomy.parse_value(raw)
    assert value == expected_value
    assert unit == expected_unit


@pytest.mark.parametrize(
    "title,category",
    [
        ("Non-Farm Employment Change", "NFP"),
        ("Core CPI m/m", "CORE_CPI"),
        ("CPI y/y", "CPI"),
        ("ISM Manufacturing PMI", "PMI_MANUF"),
        ("ISM Services PMI", "PMI_SERV"),
        ("Unemployment Rate", "UNEMPLOYMENT"),
        ("Trade Balance", "TRADE_BALANCE"),
        ("Federal Funds Rate", "RATE_DECISION"),
        ("Qualcosa di non modellato", "OTHER"),
    ],
)
def test_classify_category(title, category):
    assert taxonomy.classify_category(title) == category


def test_impact_normalization():
    assert taxonomy.normalize_impact("High") == "RED"
    assert taxonomy.normalize_impact("Medium") == "ORANGE"
    assert taxonomy.normalize_impact("Low") == "YELLOW"
    assert taxonomy.normalize_impact(None) == "YELLOW"


def test_inverted_indicator_direction():
    """Disoccupazione: un valore piu' ALTO e' negativo per la valuta."""
    assert taxonomy.category_direction("UNEMPLOYMENT") == -1
    assert taxonomy.category_direction("NFP") == 1


def test_big_beat_is_positive_for_currency(session):
    event = make_event(
        session,
        title="Non-Farm Employment Change",
        currency="USD",
        actual=350.0,
        forecast=180.0,
        previous=175.0,
    )
    result = surprise.compute_surprise(session, event)
    assert result.label == surprise.BIG_BEAT
    assert result.z > 2
    assert result.impact > 0


def test_unemployment_beat_is_negative_for_currency(session):
    """Disoccupazione sopra le attese => impulso NEGATIVO sulla valuta."""
    event = make_event(
        session,
        title="Unemployment Rate",
        currency="USD",
        actual=4.5,
        forecast=4.1,
        previous=4.1,
    )
    result = surprise.compute_surprise(session, event)
    assert result.z < 0
    assert result.impact < 0
    assert result.label in (surprise.MISS, surprise.BIG_MISS)


def test_pmi_above_forecast_but_below_50_is_halved(session):
    strong = make_event(
        session,
        title="ISM Manufacturing PMI",
        currency="USD",
        actual=52.5,
        forecast=50.5,
        previous=50.0,
        hours_ago=2,
    )
    weak = make_event(
        session,
        title="Manufacturing PMI",
        currency="EUR",
        actual=48.5,
        forecast=46.5,
        previous=46.0,
        hours_ago=2,
    )
    strong_result = surprise.compute_surprise(session, strong)
    weak_result = surprise.compute_surprise(session, weak)
    assert strong_result.impact > weak_result.impact
    assert any("livello neutro" in n for n in weak_result.notes)


def test_no_forecast_falls_back_to_previous(session):
    event = make_event(
        session,
        title="Retail Sales m/m",
        currency="GBP",
        actual=1.2,
        forecast=None,
        previous=0.2,
    )
    result = surprise.compute_surprise(session, event)
    assert result.reference == "previous"
    assert result.impact > 0


def test_event_without_actual_is_not_scored(session):
    event = make_event(
        session,
        title="CPI y/y",
        currency="EUR",
        actual=None,
        forecast=2.1,
        hours_ago=-5,
    )
    result = surprise.compute_surprise(session, event)
    assert result.label == surprise.NOT_RELEASED
    assert result.impact == 0.0


def test_decay_reduces_impact_over_time():
    from datetime import timedelta

    from app.storage.repositories import now_utc

    fresh = surprise.decay_factor(now_utc(), "ciclici")
    old = surprise.decay_factor(now_utc() - timedelta(days=5), "ciclici")
    assert fresh == pytest.approx(1.0, abs=1e-6)
    assert old == pytest.approx(0.5, abs=1e-3)  # half-life di 5 giorni


def test_policy_events_decay_slower():
    from datetime import timedelta

    from app.storage.repositories import now_utc

    moment = now_utc() - timedelta(days=10)
    assert surprise.decay_factor(moment, "policy") > surprise.decay_factor(moment, "ciclici")


def test_titoli_italiani_del_calendario_mt5_classificati():
    """Il calendario MQL5 si localizza nella lingua del terminale: i nomi
    italiani sono REALI, letti dall'archivio dell'utente (broker in italiano).
    Prima di questi pattern finivano tutti in OTHER e la rigiocata non
    ricostruiva nessun tasso di policy."""
    casi = {
        "Decisione del Tasso di Interesse della Fed": "RATE_DECISION",
        "Decisione di Tasso di Interesse BoJ": "RATE_DECISION",
        "Conferenza Stampa del FOMC": "MONETARY_STATEMENT",
        "Indice dei Prezzi CORE PCE a/a": "CORE_CPI",
        "Indice dei Prezzi al Consumo a/a": "CPI",
        "ADP Variazione dell'Occupazione non Agricola": "NFP",
        "PMI Manifatturiero": "PMI_MANUF",
        "Indicatore di Fiducia dei Consumatori": "CONFIDENCE",
        "Tasso di Disoccupazione": "UNEMPLOYMENT",
        "PIL t/t": "GDP",
        "Vendite al Dettaglio m/m": "RETAIL_SALES",
        "Bilancia Commerciale": "TRADE_BALANCE",
    }
    for titolo, atteso in casi.items():
        assert taxonomy.classify_category(titolo) == atteso, titolo


def test_confini_di_parola_sui_pattern_brevi():
    """'PIL' non deve accendersi dentro 'Pilastro', ne' 'IPC' dentro altre parole."""
    assert taxonomy.classify_category("Pilastro dell'economia") == "OTHER"
    assert taxonomy.classify_category("Partecipazione") == "OTHER"


def test_inglese_ancora_riconosciuto_dopo_le_aggiunte():
    assert taxonomy.classify_category("Nonfarm Payrolls") == "NFP"
    assert taxonomy.classify_category("Federal Funds Rate") == "RATE_DECISION"


def test_tassi_bce_riconosciuti_senza_parola_decisione():
    """I tre tassi BCE hanno nomi propri: prima finivano in OTHER e l'EUR era
    l'unica valuta senza tasso ricostruito nella rigiocata."""
    for titolo in [
        "Tasso di Rifinanziamento Principale BCE",
        "Tasso sui Depositi BCE",
        "Deposit Facility Rate",
    ]:
        assert taxonomy.classify_category(titolo) == "RATE_DECISION", titolo
