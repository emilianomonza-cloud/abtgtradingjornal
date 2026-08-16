"""Inizializzazione del database SQLite e gestione delle sessioni."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_config, get_settings
from .models import Base, PolicyRate

logger = logging.getLogger(__name__)

_engine: Optional[Engine] = None
_SessionFactory: Optional[sessionmaker] = None


def _create_engine() -> Engine:
    settings = get_settings()
    url = f"sqlite:///{settings.database_file}"
    engine = create_engine(
        url,
        future=True,
        echo=False,
        connect_args={"check_same_thread": False, "timeout": 20},
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # pragma: no cover - infrastruttura
        cursor = dbapi_connection.cursor()
        # WAL: letture concorrenti (API) durante le scritture dello scheduler.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        # Un import massivo tiene la transazione di scrittura aperta per minuti
        # mentre i job dello scheduler provano a scrivere (e viceversa). Senza
        # busy_timeout SQLite risponde SQLITE_BUSY dopo pochi secondi e la
        # richiesta muore con un 500. Con 120s ciascuno aspetta il proprio turno.
        cursor.execute("PRAGMA busy_timeout=120000")
        cursor.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _create_engine()
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Sessione transazionale: commit su successo, rollback su eccezione."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(seed: bool = True) -> None:
    """Crea le tabelle e (opzionalmente) inserisce i tassi di bootstrap."""
    Base.metadata.create_all(get_engine())
    _migrate_schema(get_engine())
    logger.info("Schema database pronto (%s)", get_settings().database_file)
    if seed:
        seed_policy_rates()


def _migrate_schema(engine: Engine) -> None:
    """Aggiunge ai database esistenti le colonne introdotte dopo la creazione.

    `create_all` crea solo le tabelle mancanti: su un archivio gia' popolato una
    colonna nuova nel modello non esisterebbe nella tabella e ogni SELECT
    fallirebbe, costringendo a rifare gli import. Qui si confronta lo schema
    reale con i modelli e si esegue ALTER TABLE ADD COLUMN per le sole colonne
    opzionali mancanti: nessuna riga viene toccata, niente viene cancellato.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            presenti = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in presenti:
                    continue
                if not column.nullable and column.default is None and column.server_default is None:
                    # Una colonna obbligatoria senza default non e' aggiungibile
                    # in automatico senza inventare valori: meglio fermarsi e
                    # dirlo che riempire l'archivio di dati fittizi.
                    raise RuntimeError(
                        f"Migrazione impossibile: la colonna obbligatoria "
                        f"{table.name}.{column.name} manca nel database esistente "
                        "e non ha un valore di default."
                    )
                tipo = column.type.compile(engine.dialect)
                conn.execute(
                    text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {tipo}")
                )
                logger.info(
                    "Migrazione schema: aggiunta colonna %s.%s (%s)",
                    table.name, column.name, tipo,
                )


def seed_policy_rates() -> int:
    """Inserisce i tassi di policy iniziali da sources.yaml se la tabella e' vuota.

    Non sovrascrive mai valori gia' presenti: l'operatore resta la fonte di verita'
    (POST /api/v1/admin/rates).
    """
    config = get_config()
    bootstrap = (config.sources.get("tassi_policy", {}) or {}).get("bootstrap", {}) or {}
    inserted = 0
    with session_scope() as session:
        for currency, spec in bootstrap.items():
            existing = session.query(PolicyRate).filter_by(currency=currency).one_or_none()
            if existing is not None:
                continue
            effective = None
            raw_date = spec.get("aggiornato")
            if raw_date:
                try:
                    effective = datetime.strptime(str(raw_date), "%Y-%m-%d")
                except ValueError:
                    effective = None
            session.add(
                PolicyRate(
                    currency=currency,
                    central_bank=str(spec.get("banca", "")),
                    rate=float(spec.get("tasso", 0.0)),
                    effective_date=effective,
                    source=str(spec.get("fonte", "bootstrap")),
                )
            )
            inserted += 1
    if inserted:
        logger.info("Inseriti %d tassi di policy di bootstrap (da verificare)", inserted)
    return inserted


def reset_db() -> None:
    """Distrugge e ricrea lo schema. Usato dai test."""
    Base.metadata.drop_all(get_engine())
    Base.metadata.create_all(get_engine())


def _reset_engine_for_tests() -> None:
    """Forza la ricreazione di engine/sessioni (usato dalle fixture di test)."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
