"""Test dell'import dello storico calendario MT5 (schema mt5-calendar-export/1).

I campioni riproducono ESATTAMENTE l'output di CalendarHistoryExporter.mq5
(mql5/Scripts/): stesse colonne CSV, stessa semantica null, stessi valori raw
in scala ×10^6. Il produttore e' versionato nel repo, quindi ogni divergenza
fra questi campioni e il suo output reale e' un bug da correggere qui, non da
tollerare.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.collectors import mt5_calendar
from app.main import app
from app.storage.models import Event


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client

# --------------------------------------------------------------------------- #
#  Campioni — un NFP con revisione, una decisione BCE, una festivita' e un
#  record con lookup metadati fallito (event_name null).
# --------------------------------------------------------------------------- #

_CSV_HEADER = (
    "value_id,event_id,time_server,time_utc,period,revision,"
    "country_code,country_name,currency,event_name,event_code,event_type,"
    "sector,importance,frequency,time_mode,unit,multiplier,digits,"
    "actual,forecast,previous,revised_previous,"
    "actual_raw,forecast_raw,previous_raw,revised_previous_raw,"
    "impact_type,source_url"
)

CSV = "﻿" + "\n".join([
    _CSV_HEADER,
    # NFP: actual 164K -> raw 164×10^6... l'unita' e' thousands, valore 164.
    "101,9001,2019-08-02T15:30:00,2019-08-02T12:30:00,2019-07-01T00:00:00,1,"
    "US,United States,USD,Nonfarm Payrolls,nonfarm-payrolls,indicator,"
    "jobs,high,month,datetime,job,thousands,0,"
    "164,165,193,190,"
    "164000000,165000000,193000000,190000000,"
    "negative,https://www.bls.gov/",
    # BCE: decisione tassi, time_utc VUOTO (offset non dichiarato).
    "102,9002,2019-09-12T14:45:00,,2019-09-01T00:00:00,0,"
    "EU,European Union,EUR,Main Refinancing Rate,main-refinancing-rate,indicator,"
    "money,high,month,datetime,percent,none,2,"
    "0.00,0.00,0.00,,"
    "0,0,0,,"
    "na,https://www.ecb.europa.eu/",
    # Festivita': niente valori, time_mode all_day.
    "103,9003,2019-08-12T00:00:00,,,0,"
    "JP,Japan,JPY,Mountain Day,mountain-day,holiday,"
    "holidays,none,none,all_day,none,none,0,"
    ",,,,"
    ",,,,"
    "na,",
    # Lookup fallito: event_name e currency vuoti -> scartato e contato.
    "104,9004,2019-08-13T10:00:00,,,0,"
    ",,,,,,"
    ",,,,,,6,"
    ",,,,"
    ",,,,"
    "na,",
]) + "\n"


def _json_records():
    return [
        {
            "value_id": 101, "event_id": 9001,
            "time_server": "2019-08-02T15:30:00", "time_utc": "2019-08-02T12:30:00",
            "period": "2019-07-01T00:00:00", "revision": 1,
            "country_code": "US", "country_name": "United States", "currency": "USD",
            "event_name": "Nonfarm Payrolls", "event_code": "nonfarm-payrolls",
            "event_type": "indicator", "sector": "jobs", "importance": "high",
            "frequency": "month", "time_mode": "datetime",
            "unit": "job", "multiplier": "thousands", "digits": 0,
            "actual": 164, "forecast": 165, "previous": 193, "revised_previous": 190,
            "actual_raw": 164000000, "forecast_raw": 165000000,
            "previous_raw": 193000000, "revised_previous_raw": 190000000,
            "impact_type": "negative", "source_url": "https://www.bls.gov/",
        },
        {
            "value_id": 102, "event_id": 9002,
            "time_server": "2019-09-12T14:45:00", "time_utc": None,
            "period": "2019-09-01T00:00:00", "revision": 0,
            "country_code": "EU", "country_name": "European Union", "currency": "EUR",
            "event_name": "Main Refinancing Rate", "event_code": "main-refinancing-rate",
            "event_type": "indicator", "sector": "money", "importance": "high",
            "frequency": "month", "time_mode": "datetime",
            "unit": "percent", "multiplier": "none", "digits": 2,
            "actual": 0.0, "forecast": 0.0, "previous": 0.0, "revised_previous": None,
            "actual_raw": 0, "forecast_raw": 0, "previous_raw": 0,
            "revised_previous_raw": None,
            "impact_type": "na", "source_url": "https://www.ecb.europa.eu/",
        },
    ]


# --------------------------------------------------------------------------- #
#  Parsing dei tre formati
# --------------------------------------------------------------------------- #


def test_csv_riconosciuto_e_normalizzato():
    items = mt5_calendar.parse_rows(CSV)
    assert len(items) == 4
    rows, warn = mt5_calendar.normalize_all(items)
    assert len(rows) == 3          # il record senza metadati e' scartato
    assert warn["senza_metadati_evento"] == 1


def test_json_array_riconosciuto():
    payload = json.dumps(_json_records())
    rows, _ = mt5_calendar.normalize_all(mt5_calendar.parse_rows(payload))
    assert len(rows) == 2


def test_jsonl_riconosciuto():
    payload = "\n".join(json.dumps(r) for r in _json_records())
    rows, _ = mt5_calendar.normalize_all(mt5_calendar.parse_rows(payload))
    assert len(rows) == 2


def test_report_di_copertura_riconosciuto_con_messaggio_chiaro():
    report = json.dumps({"schema": "mt5-calendar-export/1", "coverage": {"records": 5}})
    with pytest.raises(mt5_calendar.Mt5ParseError) as exc:
        mt5_calendar.parse_rows(report)
    assert "report di copertura" in str(exc.value)


def test_contenuto_estraneo_rifiutato_col_motivo():
    with pytest.raises(mt5_calendar.Mt5ParseError) as exc:
        mt5_calendar.parse_rows("pair,timestamp,close\nEURUSD,2020-01-01,1.1")
    assert "value_id" in str(exc.value)


# --------------------------------------------------------------------------- #
#  Semantica dei campi
# --------------------------------------------------------------------------- #


def test_preferisce_time_utc_quando_presente():
    rows, warn = mt5_calendar.normalize_all(mt5_calendar.parse_rows(CSV))
    nfp = next(r for r in rows if "Nonfarm" in r["title"])
    assert nfp["timestamp_utc"] == datetime(2019, 8, 2, 12, 30)
    # La riga BCE non ha time_utc: usa time_server e viene contata.
    bce = next(r for r in rows if r["currency"] == "EUR")
    assert bce["timestamp_utc"] == datetime(2019, 9, 12, 14, 45)
    assert warn["fuso_non_dichiarato"] >= 1


def test_valori_dal_raw_senza_perdita():
    """actual_raw = intero ×10^6: la conversione deve essere esatta."""
    rows, _ = mt5_calendar.normalize_all(mt5_calendar.parse_rows(CSV))
    nfp = next(r for r in rows if "Nonfarm" in r["title"])
    assert nfp["actual"] == pytest.approx(164.0)
    assert nfp["forecast"] == pytest.approx(165.0)
    assert nfp["previous"] == pytest.approx(193.0)
    assert nfp["revised"] == pytest.approx(190.0)
    assert nfp["unit"] == "K"      # multiplier thousands


def test_zero_e_assente_distinti():
    """La BCE a 0.00% e' uno zero vero; la festivita' e' assenza."""
    rows, _ = mt5_calendar.normalize_all(mt5_calendar.parse_rows(CSV))
    bce = next(r for r in rows if r["currency"] == "EUR")
    assert bce["actual"] == pytest.approx(0.0)
    festa = next(r for r in rows if r["currency"] == "JPY")
    assert festa["actual"] is None


def test_mappa_importanza_dichiarata():
    rows, _ = mt5_calendar.normalize_all(mt5_calendar.parse_rows(CSV))
    per_valuta = {r["currency"]: r["impact"] for r in rows}
    assert per_valuta["USD"] == "RED"      # high
    assert per_valuta["EUR"] == "RED"      # high
    assert per_valuta["JPY"] == "GRAY"     # holiday


def test_orario_approssimato_contato():
    _, warn = mt5_calendar.normalize_all(mt5_calendar.parse_rows(CSV))
    assert warn["orario_approssimato"] == 1   # la festivita' all_day


def test_decisione_tassi_classificata():
    """Serve alla rigiocata storica: ricostruzione dei tassi di policy."""
    rows, _ = mt5_calendar.normalize_all(mt5_calendar.parse_rows(CSV))
    bce = next(r for r in rows if r["currency"] == "EUR")
    assert bce["category"] == "RATE_DECISION"


# --------------------------------------------------------------------------- #
#  Import completo
# --------------------------------------------------------------------------- #


def test_import_salva_calcola_sorprese_e_dichiara(session):
    report = mt5_calendar.import_mt5(session, CSV, filename="mt5_calendar_test.csv")
    session.flush()

    assert report["eventi"] == 3
    assert report["creati"] == 3
    assert session.query(Event).count() == 3

    nfp = session.query(Event).filter(Event.currency == "USD").one()
    assert nfp.source == "mt5_calendar"
    assert nfp.revised == pytest.approx(190.0)

    testo_avvisi = " ".join(report["avvisi"])
    assert "fuso" in testo_avvisi.lower() or "server di trading" in testo_avvisi
    assert "MetaQuotes" in testo_avvisi          # mappa impatto dichiarata
    assert "scartati" in testo_avvisi            # lookup falliti dichiarati


def test_offset_dichiarato_converte_in_utc():
    """Report reale del broker: declared null, osservato -9.01h. Con offset -9
    l'ora del server 14:45 diventa 23:45 UTC (utc = server - offset)."""
    rows, warn = mt5_calendar.normalize_all(
        mt5_calendar.parse_rows(CSV), gmt_offset_hours=-9.0
    )
    bce = next(r for r in rows if r["currency"] == "EUR")
    assert bce["timestamp_utc"] == datetime(2019, 9, 12, 23, 45)
    assert warn["fuso_corretto_da_offset"] >= 1
    assert warn["fuso_non_dichiarato"] == 0
    # Le righe con time_utc gia' valorizzato NON vengono toccate dall'offset.
    nfp = next(r for r in rows if "Nonfarm" in r["title"])
    assert nfp["timestamp_utc"] == datetime(2019, 8, 2, 12, 30)


def test_import_con_offset_dichiara_l_approssimazione(session):
    report = mt5_calendar.import_mt5(session, CSV, gmt_offset_hours=-9.0)
    testo = " ".join(report["avvisi"])
    assert "offset" in testo.lower()
    assert "ora legale" in testo.lower()


def test_copertura_calcolata_all_import(session):
    report = mt5_calendar.import_mt5(session, CSV)
    cop = report["copertura"]
    assert cop["per_valuta"]["USD"] == 1
    assert cop["per_impatto"]["RED"] == 2
    assert cop["per_impatto"]["GRAY"] == 1
    assert cop["con_actual"] == 2
    assert cop["con_revisione"] == 1
    assert set(cop["per_anno"].keys()) == {2019}


def test_import_ripetuto_non_duplica(session):
    mt5_calendar.import_mt5(session, CSV)
    session.flush()
    secondo = mt5_calendar.import_mt5(session, CSV)
    session.flush()
    assert secondo["creati"] == 0
    assert secondo["aggiornati"] == 3
    assert session.query(Event).count() == 3


def test_percorso_bulk_equivale_al_normale(session, monkeypatch):
    """Sopra soglia scatta il percorso a lotti: stessi risultati, stessa dedup."""
    monkeypatch.setattr(mt5_calendar, "BULK_THRESHOLD", 2)
    monkeypatch.setattr(mt5_calendar, "BULK_BATCH", 2)

    report = mt5_calendar.import_mt5(session, CSV)
    session.flush()
    assert report["creati"] == 3
    assert report["sorprese_calcolate"] == 2   # NFP e BCE hanno actual
    assert session.query(Event).count() == 3

    secondo = mt5_calendar.import_mt5(session, CSV)
    session.flush()
    assert secondo["creati"] == 0
    assert secondo["aggiornati"] == 3
    assert session.query(Event).count() == 3

    nfp = session.query(Event).filter(Event.currency == "USD").one()
    assert nfp.surprise_label is not None       # la sorpresa e' stata calcolata
    assert nfp.revised == pytest.approx(190.0)


def test_import_da_api(client):
    risposta = client.post(
        "/api/v1/admin/calendar/import-mt5",
        json={"payload": CSV, "content_type": "csv"},
    )
    assert risposta.status_code == 200
    corpo = risposta.json()
    assert corpo["eventi"] == 3
    assert corpo["metodo"] == "mt5_calendar"


def test_database_occupato_restituisce_503_spiegato(client, monkeypatch):
    """SQLITE_BUSY durante l'import non deve diventare un 500 muto."""
    from sqlalchemy.exc import OperationalError

    def esplodi(*args, **kwargs):
        raise OperationalError("stmt", {}, Exception("database is locked"))

    monkeypatch.setattr(mt5_calendar, "import_mt5", esplodi)
    risposta = client.post(
        "/api/v1/admin/calendar/import-mt5",
        json={"payload": CSV, "content_type": "csv"},
    )
    assert risposta.status_code == 503
    assert "occupato" in risposta.json()["detail"].lower()


def test_errore_inatteso_restituisce_dettaglio_leggibile(client, monkeypatch):
    def esplodi(*args, **kwargs):
        raise RuntimeError("qualcosa di inaspettato")

    monkeypatch.setattr(mt5_calendar, "import_mt5", esplodi)
    risposta = client.post(
        "/api/v1/admin/calendar/import-mt5",
        json={"payload": CSV, "content_type": "csv"},
    )
    assert risposta.status_code == 500
    assert "RuntimeError" in risposta.json()["detail"]
    assert "qualcosa di inaspettato" in risposta.json()["detail"]


def test_report_caricato_su_api_restituisce_422(client):
    report = json.dumps({"schema": "mt5-calendar-export/1", "coverage": {}})
    risposta = client.post(
        "/api/v1/admin/calendar/import-mt5",
        json={"payload": report, "content_type": "json"},
    )
    assert risposta.status_code == 422
    assert "report" in risposta.json()["detail"].lower()
