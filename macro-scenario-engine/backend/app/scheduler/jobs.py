"""SEZIONE 4.1 / 10 — Job ricorrenti (APScheduler).

Cadenze:
  * calendario: ogni `MSE_CALENDAR_REFRESH_MINUTES` (default 60);
  * documenti banche centrali: ogni `MSE_CB_REFRESH_MINUTES` (default 180);
  * scenari: ogni `MSE_SCENARIO_REFRESH_MINUTES` (default 15);
  * export file MT5: ogni `MSE_MT5_EXPORT_MINUTES` (default 1);
  * finestre evento (T−1h / T+5min / T+1h): job dedicato ogni 5 minuti che
    aggiorna il calendario solo se c'e' un evento RED nella finestra;
  * valutazione ex-post degli scenari: ogni 6 ore.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .. import pipeline
from ..calibration import evaluation as calibration
from ..collectors import calendar as calendar_collector
from ..collectors import central_banks as cb_collector
from ..config import get_config, get_settings
from ..storage.database import session_scope
from ..storage.repositories import events_between, now_utc

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None


# --------------------------------------------------------------------------- #
#  Job
# --------------------------------------------------------------------------- #


async def job_calendar() -> None:
    try:
        with session_scope() as session:
            report = await calendar_collector.collect_calendar(session)
        logger.info("Calendario aggiornato: %s", report.get("totale"))
    except Exception:  # pragma: no cover - il job non deve mai uccidere lo scheduler
        logger.exception("Job calendario fallito")


async def job_central_banks() -> None:
    try:
        with session_scope() as session:
            report = await cb_collector.collect_central_banks(session)
        logger.info("Banche centrali aggiornate: %d nuovi documenti", report["nuovi_documenti"])
    except Exception:  # pragma: no cover
        logger.exception("Job banche centrali fallito")


def job_scenarios() -> None:
    try:
        report = pipeline.refresh_in_new_session()
        logger.info("Scenari rigenerati: %s", report)
    except Exception:  # pragma: no cover
        logger.exception("Job scenari fallito")


def job_export_mt5() -> None:
    try:
        path = export_snapshot_file()
        logger.debug("Snapshot MT5 esportato in %s", path)
    except Exception:  # pragma: no cover
        logger.exception("Export snapshot MT5 fallito")


async def job_event_window() -> None:
    """Aggiorna il calendario attorno agli eventi ad alto impatto.

    Cattura `actual` e revisioni subito dopo il rilascio (T+5min, T+1h) e
    ricontrolla il forecast un'ora prima (T−1h).
    """
    try:
        now = now_utc()
        with session_scope() as session:
            events = events_between(
                session, now - timedelta(hours=1, minutes=10), now + timedelta(hours=1), None, ["RED"]
            )
            if not events:
                return
            logger.info("Finestra evento RED attiva: aggiorno calendario e scenari")
            await calendar_collector.collect_calendar(session)
        pipeline.refresh_in_new_session()
    except Exception:  # pragma: no cover
        logger.exception("Job finestra evento fallito")


def job_evaluate() -> None:
    try:
        with session_scope() as session:
            report = calibration.evaluate_due_scenarios(session)
        logger.info("Valutazione ex-post: %s", report)
    except Exception:  # pragma: no cover
        logger.exception("Job valutazione fallito")


# --------------------------------------------------------------------------- #
#  Export file per MT5 (SEZIONE 6.2, modalita' file)
# --------------------------------------------------------------------------- #


def export_snapshot_file() -> str:
    """Scrive `macro_snapshot.json` nella cartella MQL5\\Files configurata."""
    settings = get_settings()
    snapshot = pipeline.cache.mt5
    if not snapshot:
        with session_scope() as session:
            pipeline.refresh(session)
        snapshot = pipeline.cache.mt5

    target = settings.mt5_export_dir / "macro_snapshot.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(target)  # scrittura atomica: MT5 non legge mai un file a meta'
    return str(target)


# --------------------------------------------------------------------------- #
#  Avvio / arresto
# --------------------------------------------------------------------------- #


def start_scheduler() -> Optional[AsyncIOScheduler]:
    global _scheduler
    settings = get_settings()
    if not settings.scheduler_enabled:
        logger.info("Scheduler disattivato da configurazione")
        return None
    if _scheduler is not None:
        return _scheduler

    scheduler = AsyncIOScheduler(timezone="UTC")

    # I collector partono SUBITO dopo l'avvio, non alla scadenza del primo
    # intervallo: altrimenti la dashboard resterebbe vuota per un'ora e ogni
    # bias sarebbe zero, cioe' "NEUTRAL" ovunque, senza che sia chiaro perche'.
    now = datetime.now(timezone.utc)
    scheduler.add_job(
        job_calendar,
        IntervalTrigger(minutes=settings.calendar_refresh_minutes),
        id="calendario",
        next_run_time=now + timedelta(seconds=5),
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_central_banks,
        IntervalTrigger(minutes=settings.cb_refresh_minutes),
        id="banche_centrali",
        next_run_time=now + timedelta(seconds=30),
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_scenarios,
        IntervalTrigger(minutes=settings.scenario_refresh_minutes),
        id="scenari",
        # Dopo i due collector: cosi' i primi scenari nascono con i dati veri.
        next_run_time=now + timedelta(seconds=75),
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_export_mt5,
        IntervalTrigger(minutes=settings.mt5_export_minutes),
        id="export_mt5",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_event_window,
        IntervalTrigger(minutes=5),
        id="finestra_evento",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_evaluate,
        IntervalTrigger(hours=6),
        id="valutazione",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("Scheduler avviato con %d job", len(scheduler.get_jobs()))
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler arrestato")


def get_jobs_status() -> list[dict]:
    if _scheduler is None:
        return []
    return [
        {
            "id": job.id,
            "prossima_esecuzione": job.next_run_time.isoformat()
            if job.next_run_time
            else None,
        }
        for job in _scheduler.get_jobs()
    ]
