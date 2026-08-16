"""Funzioni di accesso ai dati: upsert eventi, query, stato fonti."""

from __future__ import annotations

import hashlib
import logging
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CBDocument, CurrencyScoreSnapshot, Event, SourceHealth

logger = logging.getLogger(__name__)

_WS = re.compile(r"\s+")


def naive_utc(dt: datetime) -> datetime:
    """SQLite non conserva il tz: normalizziamo tutto a UTC naive."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


_orologio_congelato: Optional[datetime] = None


def now_utc() -> datetime:
    """L'istante corrente secondo il sistema.

    Passa da qui *tutto* il motore: le query sugli eventi, il decadimento delle
    sorprese, la scadenza degli scenari. Questo permette di rigiocare il passato
    (`replay.py`) spostando un solo valore, invece di aggiungere un parametro
    "data di riferimento" a ogni funzione — e soprattutto garantisce che una
    simulazione storica non possa leggere per sbaglio dati del futuro.
    """
    if _orologio_congelato is not None:
        return _orologio_congelato
    return datetime.now(timezone.utc).replace(tzinfo=None)


@contextmanager
def clock_frozen_at(moment: datetime):
    """Congela l'orologio del sistema per la durata del blocco.

    Uso previsto: **solo** la rigiocata storica, che gira in un job dedicato e
    monotematico. Non va usato per servire richieste web: e' stato globale di
    processo e falserebbe le risposte in corso.
    """
    global _orologio_congelato
    precedente = _orologio_congelato
    _orologio_congelato = naive_utc(moment)
    try:
        yield
    finally:
        _orologio_congelato = precedente


def event_hash(currency: str, title: str, timestamp_utc: datetime) -> str:
    """Chiave di deduplica.

    Basata su valuta + titolo normalizzato + GIORNO (non ora): cosi' uno
    spostamento d'orario annunciato dalla fonte aggiorna l'evento esistente
    invece di crearne un duplicato. Il limite noto e' che due eventi con
    titolo identico nello stesso giorno e valuta vengono fusi (accade solo
    per discorsi ripetuti: documentato nel README).
    """
    normalized = _WS.sub(" ", title.strip().lower())
    day = naive_utc(timestamp_utc).strftime("%Y-%m-%d")
    raw = f"{currency.upper()}|{normalized}|{day}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def upsert_event(session: Session, data: Dict[str, Any]) -> tuple[Event, bool]:
    """Inserisce o aggiorna un evento. Ritorna (evento, creato)."""
    ts = naive_utc(data["timestamp_utc"])
    digest = data.get("hash_dedup") or event_hash(data["currency"], data["title"], ts)

    event = session.execute(
        select(Event).where(Event.hash_dedup == digest)
    ).scalar_one_or_none()

    created = False
    if event is None:
        event = Event(hash_dedup=digest)
        session.add(event)
        created = True

    event.timestamp_utc = ts
    event.currency = str(data["currency"]).upper()
    event.title = str(data["title"])[:255]
    event.category = str(data.get("category", "OTHER"))
    event.impact = str(data.get("impact", "YELLOW")).upper()
    event.source = str(data.get("source", "unknown"))
    event.unit = data.get("unit")

    # I valori numerici vengono sovrascritti solo se la nuova lettura ne ha uno:
    # un refresh che non riporta l'actual non deve cancellare quello gia' salvato.
    for field in ("actual", "forecast", "previous", "revised"):
        value = data.get(field)
        if value is not None:
            setattr(event, field, float(value))
        raw = data.get(f"{field}_raw")
        if raw not in (None, ""):
            setattr(event, f"{field}_raw", str(raw)[:32])

    return event, created


def events_between(
    session: Session,
    start: datetime,
    end: datetime,
    currencies: Optional[Sequence[str]] = None,
    impacts: Optional[Sequence[str]] = None,
) -> List[Event]:
    stmt = select(Event).where(
        Event.timestamp_utc >= naive_utc(start), Event.timestamp_utc <= naive_utc(end)
    )
    if currencies:
        stmt = stmt.where(Event.currency.in_([c.upper() for c in currencies]))
    if impacts:
        stmt = stmt.where(Event.impact.in_([i.upper() for i in impacts]))
    stmt = stmt.order_by(Event.timestamp_utc.asc())
    return list(session.execute(stmt).scalars().all())


def recent_events(
    session: Session,
    currency: str,
    lookback_days: int,
    categories: Optional[Iterable[str]] = None,
    only_released: bool = True,
) -> List[Event]:
    """Eventi passati della valuta, dal piu' recente al piu' vecchio."""
    start = now_utc() - timedelta(days=lookback_days)
    stmt = select(Event).where(
        Event.currency == currency.upper(),
        Event.timestamp_utc >= start,
        Event.timestamp_utc <= now_utc(),
    )
    if categories:
        stmt = stmt.where(Event.category.in_(list(categories)))
    if only_released:
        stmt = stmt.where(Event.actual.is_not(None))
    stmt = stmt.order_by(Event.timestamp_utc.desc())
    return list(session.execute(stmt).scalars().all())


def upcoming_events(
    session: Session,
    currencies: Sequence[str],
    hours: int,
    impacts: Optional[Sequence[str]] = None,
) -> List[Event]:
    now = now_utc()
    return events_between(session, now, now + timedelta(hours=hours), currencies, impacts)


def historical_series(
    session: Session, currency: str, category: str, limit: int = 40
) -> List[Event]:
    """Storico dei rilasci di un indicatore, per stimare la deviazione tipica."""
    stmt = (
        select(Event)
        .where(
            Event.currency == currency.upper(),
            Event.category == category,
            Event.actual.is_not(None),
            Event.forecast.is_not(None),
        )
        .order_by(Event.timestamp_utc.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars().all())


def latest_documents(
    session: Session, currency: str, lookback_days: int = 90, limit: int = 25
) -> List[CBDocument]:
    start = now_utc() - timedelta(days=lookback_days)
    stmt = (
        select(CBDocument)
        .where(CBDocument.currency == currency.upper(), CBDocument.published_at >= start)
        .order_by(CBDocument.published_at.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars().all())


def latest_scores(session: Session, currency: str, limit: int = 2) -> List[CurrencyScoreSnapshot]:
    stmt = (
        select(CurrencyScoreSnapshot)
        .where(CurrencyScoreSnapshot.currency == currency.upper())
        .order_by(CurrencyScoreSnapshot.computed_at.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars().all())


def score_at_or_before(
    session: Session, currency: str, moment: datetime
) -> Optional[CurrencyScoreSnapshot]:
    stmt = (
        select(CurrencyScoreSnapshot)
        .where(
            CurrencyScoreSnapshot.currency == currency.upper(),
            CurrencyScoreSnapshot.computed_at <= naive_utc(moment),
        )
        .order_by(CurrencyScoreSnapshot.computed_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def record_source_health(
    session: Session,
    name: str,
    status: str,
    *,
    label: str = "",
    latency_ms: Optional[float] = None,
    items: int = 0,
    message: str = "",
) -> SourceHealth:
    """Aggiorna lo stato di un collector (dashboard 'Fonti & Salute sistema')."""
    row = session.execute(
        select(SourceHealth).where(SourceHealth.name == name)
    ).scalar_one_or_none()
    if row is None:
        row = SourceHealth(name=name)
        session.add(row)

    row.label = label or row.label or name
    row.status = status
    row.last_run = now_utc()
    row.latency_ms = latency_ms
    row.items = items
    row.message = message[:1000]
    if status == "OK":
        row.last_success = row.last_run
        row.consecutive_failures = 0
    else:
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
    return row


def all_source_health(session: Session) -> List[SourceHealth]:
    return list(
        session.execute(select(SourceHealth).order_by(SourceHealth.name.asc())).scalars().all()
    )
