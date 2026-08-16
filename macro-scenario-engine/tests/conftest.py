"""Fixture comuni: database temporaneo, rete disattivata, dati di esempio."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

import pytest

# L'ambiente va impostato PRIMA di importare i moduli dell'applicazione:
# le impostazioni sono in cache (lru_cache) e vengono lette al primo import.
_TMPDIR = Path(tempfile.mkdtemp(prefix="mse-test-"))
os.environ["MSE_DB_PATH"] = str(_TMPDIR / "test.db")
os.environ["MSE_CACHE_DIR"] = str(_TMPDIR / "cache")
os.environ["MSE_MT5_FILES_DIR"] = str(_TMPDIR / "mt5")
os.environ["MSE_NETWORK_ENABLED"] = "false"
os.environ["MSE_SCHEDULER_ENABLED"] = "false"
os.environ["MSE_LLM_PROVIDER"] = "none"

from app.storage import database  # noqa: E402
from app.storage.models import Event, PolicyRate  # noqa: E402
from app.storage.repositories import now_utc, upsert_event  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _init_database() -> Iterator[None]:
    database.init_db(seed=True)
    yield


@pytest.fixture()
def session() -> Iterator:
    """Sessione isolata: lo schema viene azzerato prima di ogni test."""
    database.reset_db()
    database.seed_policy_rates()
    factory = database.get_session_factory()
    db = factory()
    try:
        yield db
        db.commit()
    finally:
        db.rollback()
        db.close()


def make_event(
    session,
    *,
    title: str,
    currency: str,
    hours_ago: float = 1.0,
    impact: str = "RED",
    actual: float | None = None,
    forecast: float | None = None,
    previous: float | None = None,
    category: str | None = None,
) -> Event:
    """Crea un evento di calendario per i test."""
    from app.engine import taxonomy

    timestamp = now_utc() - timedelta(hours=hours_ago)
    event, _ = upsert_event(
        session,
        {
            "timestamp_utc": timestamp,
            "currency": currency,
            "title": title,
            "category": category or taxonomy.classify_category(title),
            "impact": impact,
            "actual": actual,
            "forecast": forecast,
            "previous": previous,
            "source": "test",
        },
    )
    session.flush()
    return event


def set_rate(session, currency: str, rate: float, bank: str = "TEST") -> PolicyRate:
    row = session.query(PolicyRate).filter_by(currency=currency).one_or_none()
    if row is None:
        row = PolicyRate(currency=currency, rate=rate, central_bank=bank)
        session.add(row)
    row.rate = rate
    row.central_bank = bank
    row.updated_at = now_utc()
    session.flush()
    return row
