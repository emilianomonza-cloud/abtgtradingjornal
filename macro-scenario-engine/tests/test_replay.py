"""Test della rigiocata storica.

Due proprieta' non negoziabili vengono verificate qui:

  1. **Niente sguardo al futuro.** Uno scenario generato a una certa data non
     deve poter usare eventi successivi. Se questa proprieta' cade, l'hit-rate
     che ne esce e' un numero senza significato.
  2. **Niente numeri inventati.** Senza dati la rigiocata si rifiuta di partire
     invece di produrre un campione di scenari neutri.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.calibration import replay
from app.engine import scoring
from app.storage.models import Event, PriceBar, Scenario
from app.storage.repositories import clock_frozen_at, now_utc, upsert_event


# --------------------------------------------------------------------------- #
#  Aiutanti
# --------------------------------------------------------------------------- #


def evento_a(session, *, quando: datetime, title: str, currency: str, **kwargs) -> Event:
    """Evento datato in modo assoluto (i test storici non usano 'ore fa')."""
    from app.engine import taxonomy

    event, _ = upsert_event(
        session,
        {
            "timestamp_utc": quando,
            "currency": currency,
            "title": title,
            "category": kwargs.pop("category", None) or taxonomy.classify_category(title),
            "impact": kwargs.pop("impact", "RED"),
            "actual": kwargs.pop("actual", None),
            "forecast": kwargs.pop("forecast", None),
            "previous": kwargs.pop("previous", None),
            "source": "test_storico",
        },
    )
    session.flush()
    return event


def prezzi(session, pair: str, inizio: datetime, giorni: int, passo: float) -> None:
    """Serie di prezzi giornaliera con deriva costante."""
    for i in range(giorni):
        session.add(
            PriceBar(
                pair=pair,
                timestamp_utc=inizio + timedelta(days=i),
                close=1.1000 + i * passo,
                source="test",
            )
        )
    session.flush()


# --------------------------------------------------------------------------- #
#  Orologio congelato
# --------------------------------------------------------------------------- #


def test_orologio_congelato_e_ripristinato():
    momento = datetime(2019, 8, 15, 12, 0)
    prima = now_utc()
    with clock_frozen_at(momento):
        assert now_utc() == momento
    dopo = now_utc()
    assert dopo >= prima
    assert dopo != momento


def test_orologio_congelato_ripristina_anche_dopo_errore():
    """Un'eccezione dentro il blocco non deve lasciare il sistema nel passato."""
    with pytest.raises(RuntimeError):
        with clock_frozen_at(datetime(2019, 1, 1)):
            raise RuntimeError("qualcosa e' andato storto")
    assert now_utc().year >= 2024


def test_orologio_congelato_nasconde_gli_eventi_futuri(session):
    """La proprieta' fondamentale: il passato non puo' vedere il futuro."""
    base = datetime(2019, 8, 1, 12, 0)
    evento_a(session, quando=base, title="CPI y/y", currency="USD",
             actual=2.0, forecast=1.8, previous=1.8)
    evento_a(session, quando=base + timedelta(days=20), title="CPI y/y",
             currency="USD", actual=9.9, forecast=1.8, previous=2.0)

    from app.storage.repositories import recent_events

    with clock_frozen_at(base + timedelta(days=1)):
        visti = recent_events(session, "USD", 90)
    assert len(visti) == 1
    assert visti[0].actual == pytest.approx(2.0)

    with clock_frozen_at(base + timedelta(days=30)):
        visti = recent_events(session, "USD", 90)
    assert len(visti) == 2


# --------------------------------------------------------------------------- #
#  Tassi ricostruiti
# --------------------------------------------------------------------------- #


def test_tassi_ricostruiti_dalle_decisioni_di_calendario(session):
    """I tassi di oggi sarebbero un dato del futuro: si ricostruiscono."""
    evento_a(session, quando=datetime(2019, 7, 31, 18, 0),
             title="Federal Funds Rate", currency="USD",
             actual=2.25, forecast=2.25, previous=2.50)
    evento_a(session, quando=datetime(2019, 9, 18, 18, 0),
             title="Federal Funds Rate", currency="USD",
             actual=2.00, forecast=2.00, previous=2.25)

    a_agosto = replay.rates_asof(session, datetime(2019, 8, 15))
    assert a_agosto["USD"].rate == pytest.approx(2.25)

    a_ottobre = replay.rates_asof(session, datetime(2019, 10, 1))
    assert a_ottobre["USD"].rate == pytest.approx(2.00)

    # Prima di qualsiasi decisione non si inventa un tasso.
    prima = replay.rates_asof(session, datetime(2019, 1, 1))
    assert "USD" not in prima


def test_tassi_ricostruiti_non_toccano_archivio(session):
    """La ricostruzione e' solo di calcolo: i tassi correnti restano intatti."""
    from app.storage.models import PolicyRate

    originale = session.query(PolicyRate).filter_by(currency="USD").one().rate
    evento_a(session, quando=datetime(2019, 7, 31, 18, 0),
             title="Federal Funds Rate", currency="USD", actual=2.25)

    replay.rates_asof(session, datetime(2019, 8, 15))
    session.flush()

    assert session.query(PolicyRate).filter_by(currency="USD").one().rate == originale


def test_score_usa_i_tassi_passati_quando_forniti(session):
    """Passare i tassi storici deve cambiare davvero il sotto-score."""
    from app.storage.models import PolicyRate

    alto = {
        "USD": PolicyRate(currency="USD", central_bank="FED", rate=8.0,
                          updated_at=now_utc(), source="test"),
        "EUR": PolicyRate(currency="EUR", central_bank="BCE", rate=0.0,
                          updated_at=now_utc(), source="test"),
    }
    con_override = scoring.evaluate_currency(session, "USD", alto)
    senza = scoring.evaluate_currency(session, "USD")

    assert con_override.subscores["rate_differential"].value > senza.subscores[
        "rate_differential"
    ].value


# --------------------------------------------------------------------------- #
#  Rigiocata completa
# --------------------------------------------------------------------------- #


def test_rigiocata_senza_eventi_si_rifiuta(session):
    """Senza dati non si produce un campione: si dice perche'."""
    with pytest.raises(ValueError) as exc:
        replay.replay(session, datetime(2019, 8, 1), datetime(2019, 8, 31))
    assert "importa prima" in str(exc.value).lower()


def test_rigiocata_rifiuta_intervallo_invertito(session):
    evento_a(session, quando=datetime(2019, 8, 5), title="CPI y/y",
             currency="USD", actual=2.0, forecast=1.8)
    with pytest.raises(ValueError):
        replay.replay(session, datetime(2019, 8, 31), datetime(2019, 8, 1))


def test_rigiocata_genera_scenari_datati_nel_passato(session):
    inizio = datetime(2019, 8, 1)
    fine = datetime(2019, 8, 20)
    for giorno in (2, 9, 16):
        evento_a(session, quando=inizio + timedelta(days=giorno, hours=12),
                 title="Non-Farm Employment Change", currency="USD",
                 actual=200.0 + giorno, forecast=180.0, previous=190.0)
    evento_a(session, quando=datetime(2019, 7, 31, 18, 0),
             title="Federal Funds Rate", currency="USD", actual=2.25)

    report = replay.replay(session, inizio, fine, step_hours=24)

    assert report["scenari_generati"] > 0
    assert report["passi_falliti"] == 0
    assert "USD" in report["tassi_ricostruiti"]
    assert len(report["limiti"]) == 4

    scenari = session.query(Scenario).all()
    assert scenari, "la rigiocata deve storicizzare gli scenari"
    for s in scenari:
        assert inizio <= s.generated_at <= fine, "scenario datato fuori dal periodo"
        assert s.payload.get("origine") == replay.ORIGINE


def test_rigiocata_non_supera_il_presente(session):
    """Un orizzonte che finisce domani non e' valutabile: la fine viene tagliata."""
    evento_a(session, quando=now_utc() - timedelta(days=10), title="CPI y/y",
             currency="USD", actual=2.0, forecast=1.8)

    report = replay.replay(
        session,
        now_utc() - timedelta(days=12),
        now_utc() + timedelta(days=30),
        step_hours=24,
    )
    fine = datetime.fromisoformat(report["periodo"][1])
    assert fine <= now_utc() + timedelta(seconds=5)


def test_rigiocata_ripetuta_sostituisce_le_precedenti(session):
    inizio = datetime(2019, 8, 1)
    fine = datetime(2019, 8, 10)
    evento_a(session, quando=datetime(2019, 8, 2, 12, 0),
             title="Non-Farm Employment Change", currency="USD",
             actual=210.0, forecast=180.0, previous=190.0)

    primo = replay.replay(session, inizio, fine, step_hours=24)
    conteggio = session.query(Scenario).count()

    secondo = replay.replay(session, inizio, fine, step_hours=24)
    assert secondo["scenari_rimossi"] == primo["scenari_generati"]
    assert session.query(Scenario).count() == conteggio


def test_rigiocata_non_cancella_gli_scenari_veri(session):
    """Gli scenari generati in tempo reale non vanno toccati mai."""
    from app.engine import scenarios as scenario_engine

    inizio = datetime(2019, 8, 1)
    fine = datetime(2019, 8, 10)
    evento_a(session, quando=datetime(2019, 8, 2, 12, 0), title="CPI y/y",
             currency="USD", actual=2.0, forecast=1.8, previous=1.8)

    with clock_frozen_at(datetime(2019, 8, 5, 12, 0)):
        evaluations = scoring.evaluate_all(session)
        payload = scenario_engine.generate_pair_scenarios(session, "EURUSD", evaluations)
        scenario_engine.persist_scenario(session, payload)
    session.flush()
    vero_id = session.query(Scenario).one().id

    replay.replay(session, inizio, fine, step_hours=24)
    replay.replay(session, inizio, fine, step_hours=24)

    assert session.get(Scenario, vero_id) is not None


def test_rigiocata_valuta_contro_i_prezzi_importati(session):
    """Il ciclo completo: eventi storici + prezzi → scenari valutati."""
    inizio = datetime(2019, 8, 1)
    fine = datetime(2019, 8, 25)
    for giorno in (2, 9, 16):
        evento_a(session, quando=inizio + timedelta(days=giorno, hours=12),
                 title="Non-Farm Employment Change", currency="USD",
                 actual=250.0, forecast=180.0, previous=190.0)
    evento_a(session, quando=datetime(2019, 7, 31, 18, 0),
             title="Federal Funds Rate", currency="USD", actual=2.25)

    # Prezzi su tutto il periodo piu' il margine dell'orizzonte.
    prezzi(session, "EURUSD", inizio - timedelta(days=2), 60, 0.0015)

    report = replay.replay(session, inizio, fine, step_hours=24)

    assert report["scenari_generati"] > 0
    assert report["valutazione"]["valutati"] > 0, (
        "con i prezzi disponibili gli scenari scaduti devono essere valutati"
    )


def test_griglia_salta_il_fine_settimana():
    passi = replay._passi(
        datetime(2019, 8, 1), datetime(2019, 8, 14), 24, solo_giorni_feriali=True
    )
    assert all(p.weekday() < 5 for p in passi)

    con_weekend = replay._passi(
        datetime(2019, 8, 1), datetime(2019, 8, 14), 24, solo_giorni_feriali=False
    )
    assert len(con_weekend) > len(passi)


def test_griglia_ha_un_tetto_di_passi():
    """Una richiesta enorme non deve poter bloccare il processo."""
    passi = replay._passi(
        datetime(2000, 1, 1), datetime(2020, 1, 1), 1, solo_giorni_feriali=False
    )
    assert len(passi) == replay.MAX_PASSI


def test_troncamento_dichiarato_non_silenzioso(session, monkeypatch):
    """Il tetto MAX_PASSI puo' fermare una rigiocata lunga: deve dirlo e
    spiegare come completare, mai troncare in silenzio."""
    monkeypatch.setattr(replay, "MAX_PASSI", 5)
    evento_a(session, quando=datetime(2019, 8, 2, 12, 0), title="CPI y/y",
             currency="USD", actual=2.0, forecast=1.8)

    r = replay.replay(session, datetime(2019, 8, 1), datetime(2019, 8, 30), step_hours=24)
    assert r["troncato"] is True
    assert r["passi"] == 5
    assert r["periodo_effettivo"] is not None
    assert any("TRONCATO" in a for a in r["avvisi"])
    assert any("seconda rigiocata" in a for a in r["avvisi"])


def test_nessun_avviso_di_troncamento_quando_completa(session):
    evento_a(session, quando=datetime(2019, 8, 2, 12, 0), title="CPI y/y",
             currency="USD", actual=2.0, forecast=1.8)
    r = replay.replay(session, datetime(2019, 8, 1), datetime(2019, 8, 10), step_hours=24)
    assert r["troncato"] is False
    assert r["avvisi"] == []
