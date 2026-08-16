"""Test su parser dei collector, layer semantico e calibrazione."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.calibration import evaluation as calibration
from app.collectors import calendar as calendar_collector
from app.collectors.central_banks import parse_feed
from app.enrichment.rule_based import classify_text
from app.storage.models import Event, Scenario
from app.storage.repositories import now_utc

FF_JSON = json.dumps(
    [
        {
            "title": "Non-Farm Employment Change",
            "country": "USD",
            "date": "2026-08-07T12:30:00-04:00",
            "impact": "High",
            "forecast": "180K",
            "previous": "175K",
        },
        {
            "title": "Unemployment Rate",
            "country": "USD",
            "date": "2026-08-07T12:30:00-04:00",
            "impact": "High",
            "forecast": "4.1%",
            "previous": "4.1%",
        },
        {"title": "", "country": "EUR", "date": "", "impact": "Low"},
    ]
)

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>FOMC statement: Committee decides to raise the target range</title>
    <link>https://example.org/fomc-1</link>
    <pubDate>Wed, 05 Aug 2026 18:00:00 GMT</pubDate>
    <description>Inflation remains elevated. The Committee judges that further
    tightening may be appropriate to return inflation to 2 percent.</description>
  </item>
  <item>
    <title>Minutes of the June meeting</title>
    <link>https://example.org/minutes-1</link>
    <pubDate>Wed, 15 Jul 2026 18:00:00 GMT</pubDate>
    <description>Participants noted a softening labour market and downside risks
    to growth; several favoured a rate cut at an upcoming meeting.</description>
  </item>
</channel></rss>
"""


def test_forexfactory_json_parser_normalizes_rows():
    rows = calendar_collector.parse_forexfactory_json(FF_JSON, "test")
    assert len(rows) == 2  # la riga senza titolo/data viene scartata
    nfp = rows[0]
    assert nfp["currency"] == "USD"
    assert nfp["category"] == "NFP"
    assert nfp["impact"] == "RED"
    assert nfp["forecast"] == 180.0
    assert nfp["timestamp_utc"].hour == 16  # 12:30 -04:00 → 16:30 UTC
    assert nfp["actual"] is None  # limite dichiarato del feed


def test_json_parser_rejects_garbage():
    with pytest.raises(ValueError):
        calendar_collector.parse_forexfactory_json("non-json", "test")


def test_csv_import_is_the_guaranteed_fallback(session):
    payload = (
        "timestamp_utc,currency,title,impact,actual,forecast,previous\n"
        "2026-08-07T12:30:00,USD,Non-Farm Employment Change,RED,350K,180K,175K\n"
    )
    stats = calendar_collector.import_payload(session, payload, "csv")
    assert stats["creati"] == 1
    event = session.query(Event).one()
    assert event.actual == 350.0
    assert event.surprise_label == "BIG_BEAT"


def test_duplicate_import_updates_instead_of_duplicating(session):
    payload = (
        "timestamp_utc,currency,title,impact,forecast,previous\n"
        "2026-08-07T12:30:00,USD,Non-Farm Employment Change,RED,180K,175K\n"
    )
    calendar_collector.import_payload(session, payload, "csv")
    with_actual = (
        "timestamp_utc,currency,title,impact,actual,forecast,previous\n"
        "2026-08-07T12:35:00,USD,Non-Farm Employment Change,RED,350K,180K,175K\n"
    )
    calendar_collector.import_payload(session, with_actual, "csv")

    events = session.query(Event).all()
    assert len(events) == 1  # stesso giorno + stesso titolo => aggiornamento
    assert events[0].actual == 350.0


def test_rss_parser_extracts_documents():
    spec = {"banca": "FED", "valuta": "USD", "tipo_documento": "PRESS_RELEASE", "nome": "fed"}
    docs = parse_feed(RSS, spec)
    assert len(docs) == 2
    assert docs[0]["doc_type"] == "PRESS_RELEASE"
    assert docs[1]["doc_type"] == "MINUTES"
    assert docs[0]["published_at"].year == 2026


def test_rule_based_classifier_detects_hawkish_and_dovish():
    docs = parse_feed(RSS, {"banca": "FED", "valuta": "USD", "nome": "fed"})
    hawkish = classify_text(docs[0]["body"], docs[0]["title"])
    dovish = classify_text(docs[1]["body"], docs[1]["title"])

    assert hawkish.tone_label == "HAWKISH"
    assert hawkish.tone_score > 20
    assert dovish.tone_label == "DOVISH"
    assert dovish.tone_score < -20
    assert "inflazione" in hawkish.themes
    assert hawkish.summary_it and dovish.summary_it


def test_rule_based_handles_negation():
    plain = classify_text("The Committee expects further tightening.")
    negated = classify_text("The Committee does not expect further tightening.")
    assert negated.tone_score < plain.tone_score


def test_classifier_works_in_italian():
    result = classify_text(
        "Il Consiglio direttivo ritiene necessaria una stretta monetaria: "
        "le pressioni inflazionistiche restano elevate."
    )
    assert result.tone_label == "HAWKISH"


def test_document_upsert_is_idempotent(session):
    from app.enrichment.service import upsert_document

    docs = parse_feed(RSS, {"banca": "FED", "valuta": "USD", "nome": "fed"})
    _, created_first = upsert_document(session, docs[0])
    session.flush()
    _, created_second = upsert_document(session, docs[0])
    assert created_first is True
    assert created_second is False


# --------------------------------------------------------------------------- #
#  Calibrazione
# --------------------------------------------------------------------------- #


def test_reliability_reports_insufficient_sample(session):
    metrics = calibration.reliability_metrics(session)
    assert metrics["campione"] == 0
    assert metrics["campione_sufficiente"] is False
    assert "insufficiente" in metrics["avviso"]


def test_scenario_evaluation_computes_hit_and_brier(session):
    now = now_utc()
    scenario = Scenario(
        pair="EURUSD",
        horizon="h24_72",
        generated_at=now - timedelta(days=4),
        expires_at=now - timedelta(days=3),
        horizon_end=now - timedelta(days=1),
        bias=-30.0,
        bias_label="BEARISH",
        confidence="MEDIA",
        confidence_score=55.0,
        base_direction="RIBASSO",
        base_probability=0.55,
        alt_a_probability=0.25,
        alt_b_probability=0.20,
        payload={},
    )
    session.add(scenario)

    prices = (
        "pair,timestamp,close\n"
        f"EURUSD,{(now - timedelta(days=4)).strftime('%Y-%m-%d %H:%M')},1.1000\n"
        f"EURUSD,{(now - timedelta(days=1)).strftime('%Y-%m-%d %H:%M')},1.0850\n"
    )
    inserted = calibration.import_prices_csv(session, prices)
    assert inserted == 2
    session.flush()

    report = calibration.evaluate_due_scenarios(session)
    assert report["valutati"] == 1

    metrics = calibration.reliability_metrics(session)
    assert metrics["hit_rate"] == 100.0  # ribasso previsto, ribasso realizzato
    # Le metriche sono arrotondate a 3 decimali: nessuna precisione fittizia.
    assert metrics["brier"] == pytest.approx((0.55 - 1.0) ** 2, abs=1e-3)
    assert metrics["campione_sufficiente"] is False  # 1 < 30


def test_move_classification_uses_neutral_band():
    assert calibration.classify_move(0.05) == "LATERALE"
    assert calibration.classify_move(0.9) == "RIALZO"
    assert calibration.classify_move(-0.9) == "RIBASSO"


def test_mt5_price_format_is_accepted(session):
    prices = "pair,timestamp,close\nEURUSD,2026.08.07 12:30,1.0912\n"
    assert calibration.import_prices_csv(session, prices) == 1


def test_export_barre_nativo_mt5_accettato(session):
    """Il formato che il terminale produce davvero con "Esporta barre":
    TAB come separatore, intestazioni <DATE>/<TIME>/<CLOSE>, nessun simbolo.
    Senza il riconoscimento, l'import restituirebbe zero righe in silenzio."""
    prices = (
        "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
        "2019.08.01\t00:00:00\t1.1075\t1.1096\t1.1027\t1.1085\t85125\t0\t5\n"
        "2019.08.02\t00:00:00\t1.1085\t1.1116\t1.1071\t1.1108\t91022\t0\t5\n"
    )
    # Il file nativo non porta il simbolo: serve la coppia dichiarata.
    assert calibration.import_prices_csv(session, prices) == 0
    assert calibration.import_prices_csv(session, prices, default_pair="EURUSD") == 2

    from app.storage.models import PriceBar
    from datetime import datetime

    barra = (
        session.query(PriceBar)
        .filter(PriceBar.pair == "EURUSD", PriceBar.timestamp_utc == datetime(2019, 8, 1))
        .one()
    )
    assert barra.close == pytest.approx(1.1085)


def test_export_barre_giornaliero_senza_colonna_time(session):
    """L'export D1 di alcuni build omette <TIME>: la sola data deve bastare."""
    prices = (
        "<DATE>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
        "2019.08.01\t1.1075\t1.1096\t1.1027\t1.1085\t85125\t0\t5\n"
    )
    assert calibration.import_prices_csv(session, prices, default_pair="EURUSD") == 1


def test_prezzo_di_fine_orizzonte_nel_weekend_usa_la_chiusura_successiva(session):
    """Orizzonte che scade sabato: senza la ricerca in avanti lo scenario
    resterebbe non valutato per sempre (misurato: 373 venerdi' su 1.827
    scenari in un anno di rigiocata)."""
    from datetime import datetime
    from app.storage.models import PriceBar

    # Venerdi' 2 e lunedi' 5 agosto 2019: nessuna barra sabato/domenica.
    session.add(PriceBar(pair="EURUSD", timestamp_utc=datetime(2019, 8, 2), close=1.11, source="test"))
    session.add(PriceBar(pair="EURUSD", timestamp_utc=datetime(2019, 8, 5), close=1.12, source="test"))
    session.flush()

    # Venerdi' sera: entro 12h dalla barra del venerdi' (00:00).
    assert calibration.price_at(session, "EURUSD", datetime(2019, 8, 2, 11, 0)) == pytest.approx(1.11)
    # Sabato a mezzogiorno: nessuna barra entro 12h -> price_at dice None...
    sabato = datetime(2019, 8, 3, 12, 0)
    assert calibration.price_at(session, "EURUSD", sabato) is None
    # ...ma la fine orizzonte prende la prima chiusura successiva: il lunedi'.
    assert calibration.price_at_or_next(session, "EURUSD", sabato) == pytest.approx(1.12)
    # Nessuna barra entro 80 ore -> nessuna invenzione.
    assert calibration.price_at_or_next(session, "EURUSD", datetime(2019, 8, 20)) is None


def test_reliability_espone_la_distribuzione_delle_direzioni(session):
    """Un hit-rate basso e' indecifrabile senza sapere COSA prevede il modello:
    la tabella previste/realizzate rende visibile, per esempio, un base
    LATERALE sistematico contro un mercato che si muove."""
    from datetime import timedelta as td
    now = now_utc()
    for i, (prevista, close_end) in enumerate([
        ("LATERALE", 1.0850),   # mercato sceso -> miss
        ("RIBASSO", 1.0850),    # ribasso previsto e realizzato -> hit
        ("LATERALE", 1.1001),   # laterale previsto e realizzato -> hit
    ]):
        sc = Scenario(
            pair="EURUSD", horizon="h24_72",
            generated_at=now - td(days=6 + i), expires_at=now - td(days=5 + i),
            horizon_end=now - td(days=4 + i),
            bias=0.0, bias_label="NEUTRAL", confidence="MEDIA", confidence_score=50.0,
            base_direction=prevista, base_probability=0.5,
            alt_a_probability=0.3, alt_b_probability=0.2, payload={},
        )
        session.add(sc)
        session.flush()
        prices = (
            "pair,timestamp,close\n"
            f"EURUSD,{(now - td(days=6 + i)).strftime('%Y-%m-%d %H:%M')},1.1000\n"
            f"EURUSD,{(now - td(days=4 + i)).strftime('%Y-%m-%d %H:%M')},{close_end}\n"
        )
        calibration.import_prices_csv(session, prices)
    session.flush()
    calibration.evaluate_due_scenarios(session)

    m = calibration.reliability_metrics(session)
    dirs = m["direzioni"]
    assert dirs["previste"] == {"LATERALE": 2, "RIBASSO": 1}
    assert dirs["realizzate"]["RIBASSO"] == 2
    assert dirs["hit_per_direzione_prevista"]["RIBASSO"]["hit_rate"] == 100.0
    assert dirs["hit_per_direzione_prevista"]["LATERALE"]["hit_rate"] == 50.0
