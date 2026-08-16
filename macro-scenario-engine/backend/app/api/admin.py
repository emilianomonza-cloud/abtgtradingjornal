"""Endpoint amministrativi: refresh, import dati, aggiornamento tassi."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .. import pipeline
from ..calibration import evaluation as calibration
from ..calibration import replay
from ..collectors import calendar as calendar_collector
from ..collectors import central_banks as cb_collector
from ..collectors import ff_range
from ..collectors import mt5_calendar
from ..config import get_config, reload_config
from ..engine.taxonomy import clear_taxonomy_cache
from ..enrichment.rule_based import clear_lexicon_cache
from ..enrichment.service import reclassify_all
from ..storage.models import PolicyRate
from ..storage.repositories import naive_utc
from .deps import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["amministrazione"])


class PolicyRateIn(BaseModel):
    """Aggiornamento manuale di un tasso di policy (SEZIONE 4.2)."""

    currency: str = Field(..., min_length=3, max_length=8)
    rate: float
    central_bank: Optional[str] = None
    previous_rate: Optional[float] = None
    effective_date: Optional[datetime] = None
    next_meeting: Optional[datetime] = None
    source: str = "manuale"


class PolicyRatesIn(BaseModel):
    rates: List[PolicyRateIn]


class ImportIn(BaseModel):
    """Import testuale (CSV o JSON) di eventi di calendario."""

    content_type: str = Field("csv", pattern="^(csv|json)$")
    payload: str


class PricesIn(BaseModel):
    """Import prezzi per la calibrazione ex-post."""

    pair: str = ""
    payload: str


class FFRangeIn(BaseModel):
    """Import dello storico calendario da una pagina Forex Factory con `range=`."""

    payload: str
    # Serve solo quando si importa l'HTML della tabella renderizzata, che non
    # riporta l'anno. Con l'estrazione dello snippet e' superfluo.
    year: Optional[int] = None
    # La tabella renderizzata contiene solo una frazione degli eventi: va
    # richiesta esplicitamente, non deve poter partire per ripiego.
    consenti_tabella: bool = False


class ReplayIn(BaseModel):
    """Rigiocata storica: genera scenari nel passato e li valuta."""

    start: datetime
    end: datetime
    step_hours: int = Field(24, ge=1, le=168)
    horizon: Optional[str] = None
    solo_giorni_feriali: bool = True
    sostituisci: bool = True


@router.post("/refresh", summary="Ricalcola score e scenari")
def refresh(horizon: Optional[str] = None, db: Session = Depends(get_db)) -> Dict[str, Any]:
    config = get_config()
    if horizon and horizon not in config.horizons:
        raise HTTPException(status_code=400, detail=f"Orizzonte sconosciuto: {horizon}")
    return pipeline.refresh(db, horizon)


@router.post("/collect", summary="Esegue i collector (calendario + banche centrali)")
async def collect(db: Session = Depends(get_db)) -> Dict[str, Any]:
    calendar_report = await calendar_collector.collect_calendar(db)
    cb_report = await cb_collector.collect_central_banks(db)
    db.flush()
    refresh_report = pipeline.refresh(db)
    return {
        "calendario": calendar_report,
        "banche_centrali": cb_report,
        "ricalcolo": refresh_report,
    }


@router.post("/rates", summary="Aggiorna i tassi di policy")
def update_rates(body: PolicyRatesIn, db: Session = Depends(get_db)) -> Dict[str, Any]:
    config = get_config()
    updated = []
    for item in body.rates:
        currency = item.currency.upper()
        row = db.query(PolicyRate).filter_by(currency=currency).one_or_none()
        if row is None:
            row = PolicyRate(currency=currency, rate=item.rate, central_bank="")
            db.add(row)
        if row.rate != item.rate:
            row.previous_rate = row.rate
        row.rate = item.rate
        row.central_bank = (
            item.central_bank or row.central_bank or config.central_banks.get(currency, "")
        )
        if item.previous_rate is not None:
            row.previous_rate = item.previous_rate
        if item.effective_date is not None:
            row.effective_date = naive_utc(item.effective_date)
        if item.next_meeting is not None:
            row.next_meeting = naive_utc(item.next_meeting)
        row.source = item.source
        updated.append(currency)

    db.flush()
    pipeline.refresh(db)
    return {"aggiornati": updated}


@router.post("/calendar/import", summary="Import manuale eventi (CSV o JSON)")
def import_calendar(body: ImportIn, db: Session = Depends(get_db)) -> Dict[str, Any]:
    stats = calendar_collector.import_payload(db, body.payload, body.content_type)
    db.flush()
    pipeline.refresh(db)
    return stats


@router.post("/calendar/import-file", summary="Import manuale eventi da file CSV")
async def import_calendar_file(
    file: UploadFile = File(...), db: Session = Depends(get_db)
) -> Dict[str, Any]:
    payload = (await file.read()).decode("utf-8", errors="replace")
    content_type = "json" if (file.filename or "").endswith(".json") else "csv"
    stats = calendar_collector.import_payload(db, payload, content_type)
    db.flush()
    pipeline.refresh(db)
    return stats


@router.post(
    "/calendar/import-forexfactory",
    summary="Import storico da pagina Forex Factory (estrazione JSON o HTML salvato)",
)
def import_forexfactory(body: FFRangeIn, db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        stats = ff_range.import_range(
            db, body.payload, year=body.year, consenti_tabella=body.consenti_tabella
        )
    except ff_range.RangeParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.flush()
    pipeline.refresh(db)
    return stats


@router.post(
    "/calendar/import-forexfactory-file",
    summary="Import storico Forex Factory da file (.json dello snippet o .html salvato)",
)
async def import_forexfactory_file(
    file: UploadFile = File(...),
    year: Optional[int] = None,
    consenti_tabella: bool = False,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    payload = (await file.read()).decode("utf-8", errors="replace")
    try:
        stats = ff_range.import_range(
            db, payload, year=year, consenti_tabella=consenti_tabella
        )
    except ff_range.RangeParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.flush()
    pipeline.refresh(db)
    return stats


@router.post(
    "/calendar/import-mt5",
    summary="Import storico dal terminale MT5 (export di CalendarHistoryExporter.mq5)",
)
def import_mt5(
    body: ImportIn,
    gmt_offset_hours: Optional[float] = None,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    try:
        stats = mt5_calendar.import_mt5(db, body.payload, gmt_offset_hours=gmt_offset_hours)
        db.flush()
        pipeline.refresh(db)
    except mt5_calendar.Mt5ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OperationalError as exc:
        # SQLITE_BUSY e simili: il database era occupato da un altro processo
        # (tipicamente un job dello scheduler). Non e' un dato corrotto.
        raise HTTPException(
            status_code=503,
            detail=(
                "Database occupato da un'altra operazione (probabilmente un job "
                "automatico). Riprova fra qualche istante: nessun dato e' stato "
                f"perso. Dettaglio tecnico: {exc.orig!s}"
            ),
        ) from exc
    except Exception as exc:  # noqa: BLE001 - l'errore DEVE arrivare leggibile all'utente
        logger.exception("Import MT5 fallito")
        raise HTTPException(
            status_code=500,
            detail=f"Import fallito: {type(exc).__name__}: {exc}",
        ) from exc
    return stats


@router.post(
    "/calendar/import-mt5-file",
    summary="Import storico MT5 da file (.csv, .json o .jsonl dell'exporter)",
)
async def import_mt5_file(
    file: UploadFile = File(...),
    gmt_offset_hours: Optional[float] = None,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    payload = (await file.read()).decode("utf-8-sig", errors="replace")
    try:
        stats = mt5_calendar.import_mt5(
            db, payload, filename=file.filename or "", gmt_offset_hours=gmt_offset_hours
        )
        db.flush()
        pipeline.refresh(db)
    except mt5_calendar.Mt5ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OperationalError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Database occupato da un'altra operazione (probabilmente un job "
                "automatico). Riprova fra qualche istante: nessun dato e' stato "
                f"perso. Dettaglio tecnico: {exc.orig!s}"
            ),
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Import MT5 da file fallito")
        raise HTTPException(
            status_code=500,
            detail=f"Import fallito: {type(exc).__name__}: {exc}",
        ) from exc
    return stats


@router.post(
    "/replay",
    summary="Rigioca il passato per ottenere scenari valutabili (richiede storico + prezzi)",
)
def replay_history(body: ReplayIn, db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        return replay.replay(
            db,
            start=body.start,
            end=body.end,
            step_hours=body.step_hours,
            horizon=body.horizon,
            solo_giorni_feriali=body.solo_giorni_feriali,
            sostituisci=body.sostituisci,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/prices/import", summary="Import prezzi per la calibrazione")
def import_prices(body: PricesIn, db: Session = Depends(get_db)) -> Dict[str, Any]:
    inserted = calibration.import_prices_csv(db, body.payload, body.pair.upper())
    db.flush()
    evaluated = calibration.evaluate_due_scenarios(db)
    return {"prezzi_inseriti": inserted, "valutazione": evaluated}


@router.post("/prices/import-file", summary="Import prezzi da file CSV (export MT5)")
async def import_prices_file(
    file: UploadFile = File(...), pair: str = "", db: Session = Depends(get_db)
) -> Dict[str, Any]:
    payload = (await file.read()).decode("utf-8", errors="replace")
    inserted = calibration.import_prices_csv(db, payload, pair.upper())
    db.flush()
    evaluated = calibration.evaluate_due_scenarios(db)
    return {"prezzi_inseriti": inserted, "valutazione": evaluated}


@router.post("/evaluate", summary="Valuta ex-post gli scenari scaduti")
def evaluate(db: Session = Depends(get_db)) -> Dict[str, Any]:
    return calibration.evaluate_due_scenarios(db)


@router.post("/reload-config", summary="Ricarica i file YAML di configurazione")
def reload_configuration(db: Session = Depends(get_db)) -> Dict[str, Any]:
    config = reload_config()
    clear_taxonomy_cache()
    clear_lexicon_cache()
    pipeline.refresh(db)
    return {
        "coppie": config.pairs,
        "valute": config.all_currencies,
        "pesi": config.subscore_weights,
    }


@router.post("/reclassify", summary="Riclassifica i documenti archiviati")
def reclassify(limit: int = 200, db: Session = Depends(get_db)) -> Dict[str, Any]:
    count = reclassify_all(db, limit)
    db.flush()
    pipeline.refresh(db)
    return {"documenti_riclassificati": count}
