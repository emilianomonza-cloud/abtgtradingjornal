"""Dipendenze condivise degli endpoint FastAPI."""

from __future__ import annotations

from typing import Iterator

from sqlalchemy.orm import Session

from ..storage.database import get_session_factory


def get_db() -> Iterator[Session]:
    """Sessione per richiesta: commit su successo, rollback su errore."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
