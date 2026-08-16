"""Test end-to-end sugli endpoint HTTP e sulla dashboard."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["stato"] in ("OK", "DEGRADED")
    assert "disclaimer" in body


def test_currency_scores(client):
    body = client.get("/api/v1/currencies/scores").json()
    assert "USD" in body["valute"]
    usd = body["valute"]["USD"]
    assert set(usd["sottoscore"]) == {
        "rate_differential",
        "inflation_regime",
        "growth_momentum",
        "surprise",
        "cb_stance",
        "external_balance",
    }
    assert abs(sum(body["pesi"].values()) - 1.0) < 1e-6


def test_pair_scenarios(client):
    body = client.get("/api/v1/pairs/EURUSD/scenarios").json()
    assert body["coppia"] == "EURUSD"
    assert len(body["scenari"]) == 3
    assert sum(s["probabilita"] for s in body["scenari"]) == pytest.approx(100.0, abs=0.2)


def test_unknown_pair_is_404(client):
    assert client.get("/api/v1/pairs/ZZZQQQ/scenarios").status_code == 404


def test_unknown_horizon_is_400(client):
    assert client.get("/api/v1/pairs/EURUSD/scenarios?horizon=domani").status_code == 400


def test_mt5_snapshot_schema(client):
    body = client.get("/api/v1/mt5/snapshot").json()
    assert body["v"] == 1
    assert body["ccy"] and body["pairs"]
    pair = body["pairs"][0]
    for key in ("p", "bias", "p0", "conf", "nt", "redm", "act"):
        assert key in pair
    assert -100 <= pair["bias"] <= 100
    assert pair["nt"] in (0, 1)


def test_mt5_snapshot_is_fast(client):
    """Criterio di accettazione: risposta < 200 ms con dati in cache."""
    import time

    client.get("/api/v1/mt5/snapshot")  # scalda la cache
    start = time.perf_counter()
    response = client.get("/api/v1/mt5/snapshot")
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert response.status_code == 200
    assert elapsed_ms < 200, f"snapshot servito in {elapsed_ms:.0f} ms"


def test_calendar_import_and_read(client):
    payload = {
        "content_type": "csv",
        "payload": (
            "timestamp_utc,currency,title,impact,actual,forecast,previous\n"
            "2026-08-07T12:30:00,USD,Non-Farm Employment Change,RED,350K,180K,175K\n"
        ),
    }
    stats = client.post("/api/v1/admin/calendar/import", json=payload).json()
    assert stats["creati"] + stats["aggiornati"] == 1

    body = client.get("/api/v1/calendar?from=2026-08-01T00:00:00&to=2026-08-31T00:00:00").json()
    titles = [e["title"] for e in body["eventi"]]
    assert "Non-Farm Employment Change" in titles
    event = next(e for e in body["eventi"] if e["title"] == "Non-Farm Employment Change")
    assert event["playbook"]["nome"] == "Non-Farm Payrolls"
    assert event["sorpresa"]["classificazione"] == "BIG_BEAT"


def test_rates_update(client):
    body = client.post(
        "/api/v1/admin/rates",
        json={"rates": [{"currency": "USD", "rate": 5.25, "central_bank": "FED"}]},
    ).json()
    assert body["aggiornati"] == ["USD"]
    banks = client.get("/api/v1/central-banks").json()
    fed = next(b for b in banks["banche"] if b["valuta"] == "USD")
    assert fed["tasso_policy"]["rate"] == 5.25


def test_chains_endpoint_exposes_the_model(client):
    body = client.get("/api/v1/chains").json()
    ids = {c["id"] for c in body["catene"]}
    assert {"TASSI_CAPITALI_CAMBIO", "MONETARIA_ESPANSIVA", "BILANCIA_PAGAMENTI"} <= ids


def test_reliability_endpoint(client):
    body = client.get("/api/v1/reliability").json()
    assert "campione" in body and "avviso" in body


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/calendario",
        "/scenari",
        "/banche-centrali",
        "/fonti",
        "/affidabilita",
        "/storico",
    ],
)
def test_dashboard_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "DISCLAIMER" in response.text


def test_dashboard_always_shows_the_disclaimer(client):
    text = client.get("/").text
    assert "Non costituiscono consulenza finanziaria" in text


def test_pagina_storico_dichiara_i_limiti_della_rigiocata(client):
    """La rigiocata non deve poter passare per un backtest di strategia."""
    text = client.get("/storico").text
    assert "non e' un backtest" in text.lower() or "non e' un backtest di strategia" in text.lower()
    assert "Nessun costo di esecuzione" in text


def test_import_storico_da_api(client):
    """L'endpoint accetta l'estrazione dello snippet e riporta cosa ha fatto."""
    from tests.test_ff_range import ESTRAZIONE

    response = client.post(
        "/api/v1/admin/calendar/import-forexfactory", json={"payload": ESTRAZIONE}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["eventi"] == 3
    assert body["metodo"] == "stato"
    assert body["avvisi"]


def test_import_storico_contenuto_ignoto_restituisce_422(client):
    """Un file sbagliato deve dirlo, non fallire in silenzio con zero eventi."""
    response = client.post(
        "/api/v1/admin/calendar/import-forexfactory",
        json={"payload": "<html><body>pagina sbagliata</body></html>"},
    )
    assert response.status_code == 422
    assert "parser" in response.json()["detail"].lower()
