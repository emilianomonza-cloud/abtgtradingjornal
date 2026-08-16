"""SEZIONE 5 — Dashboard server-side (Jinja2).

Scelta architetturale: rendering server-side invece di una SPA React.
Motivazione (richiesta dalla SEZIONE 2): zero build step, zero node_modules,
un solo processo da avviare, nessuna divergenza di versione fra API e UI.
La densita' informativa richiesta (tabelle, card, countdown) non richiede uno
stato client complesso: bastano un fetch periodico e qualche countdown in JS.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import pipeline
from ..api.deps import get_db
from ..calibration import evaluation as calibration
from ..calibration import replay
from ..config import get_config, get_settings
from ..engine import causal_chains, playbooks
from ..engine.surprise import compute_surprise
from ..scheduler.jobs import get_jobs_status
from ..storage.models import Event, PolicyRate, PriceBar, Scenario, ScenarioOutcome
from ..storage.repositories import all_source_health, events_between, latest_documents, now_utc

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

router = APIRouter(tags=["dashboard"])

DISCLAIMER = (
    "Scenari probabilistici generati da modello. Non costituiscono consulenza "
    "finanziaria. La probabilita' e' una stima soggetta a errore."
)


def _base_context(active: str) -> Dict[str, Any]:
    """Dati comuni a tutte le pagine.

    La `request` NON entra qui: dalla versione 1.x di Starlette va passata come
    primo argomento a TemplateResponse, ed e' quella la forma usata sotto
    (compatibile anche con le versioni precedenti).
    """
    config = get_config()
    return {
        "active": active,
        "disclaimer": DISCLAIMER,
        "config": config,
        "adesso": now_utc(),
        "llm_attivo": get_settings().llm_enabled,
    }


@router.get("/", response_class=HTMLResponse, summary="Overview")
def overview(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    pipeline.ensure_fresh(db)
    scores = pipeline.cache.scores
    scenarios = pipeline.cache.scenarios
    snapshot = pipeline.cache.mt5

    ordered = sorted(
        scores.values(), key=lambda s: s.get("score", 0.0), reverse=True
    )
    context = _base_context("overview")
    context.update(
        {
            # Senza eventi in archivio ogni score e' zero e ogni bias risulta
            # NEUTRAL: non e' un'analisi, e' assenza di dati. Va detto.
            "eventi_totali": db.query(Event).count(),
            "valute": ordered,
            "coppie": [scenarios[p] for p in get_config().pairs if p in scenarios],
            "eventi": snapshot.get("events", [])[:8],
            "aggiornato_il": pipeline.cache.updated_at,
        }
    )
    return templates.TemplateResponse(request=request, name="overview.html", context=context)


@router.get("/calendario", response_class=HTMLResponse, summary="Calendario")
def calendar_page(
    request: Request,
    giorni: int = 7,
    impatto: str = "",
    valuta: str = "",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    config = get_config()
    impacts = [i for i in impatto.split(",") if i] or None
    currencies = [c for c in valuta.split(",") if c] or None
    events = events_between(
        db,
        now_utc() - timedelta(days=2),
        now_utc() + timedelta(days=max(1, min(giorni, 30))),
        currencies,
        impacts,
    )

    rows: List[Dict[str, Any]] = []
    now = now_utc()
    for event in events:
        book = playbooks.playbook_for(event.category, event.currency)
        rows.append(
            {
                "evento": event,
                "playbook": book,
                "finestra": playbooks.window_for_event(event),
                "minuti": int((event.timestamp_utc - now).total_seconds() // 60),
                "sorpresa": compute_surprise(db, event).to_dict()
                if event.actual is not None
                else None,
                "catena": causal_chains.CHAINS.get(str(book.get("catena_attesa") or "")),
            }
        )

    context = _base_context("calendario")
    context.update(
        {
            "righe": rows,
            "giorni": giorni,
            "filtro_impatto": impatto,
            "filtro_valuta": valuta,
            "valute": config.currencies,
        }
    )
    return templates.TemplateResponse(request=request, name="calendario.html", context=context)


@router.get("/scenari", response_class=HTMLResponse, summary="Scenari")
def scenarios_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    pipeline.ensure_fresh(db)
    config = get_config()
    scenarios = pipeline.cache.scenarios
    storico = calibration.reliability_metrics(db)

    context = _base_context("scenari")
    context.update(
        {
            "coppie": [scenarios[p] for p in config.pairs if p in scenarios],
            "affidabilita": storico,
        }
    )
    return templates.TemplateResponse(request=request, name="scenari.html", context=context)


@router.get("/banche-centrali", response_class=HTMLResponse, summary="Banche centrali")
def banks_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    pipeline.ensure_fresh(db)
    config = get_config()
    rates = {r.currency: r for r in db.query(PolicyRate).all()}

    banche = []
    for currency, bank in config.central_banks.items():
        documents = latest_documents(db, currency, lookback_days=180, limit=6)
        score = pipeline.cache.scores.get(currency, {})
        stance = (score.get("sottoscore", {}) or {}).get("cb_stance", {})
        banche.append(
            {
                "banca": bank,
                "valuta": currency,
                "tasso": rates.get(currency),
                "stance": stance,
                "documenti": documents,
            }
        )

    context = _base_context("banche")
    context.update({"banche": banche})
    return templates.TemplateResponse(request=request, name="banche.html", context=context)


@router.get("/fonti", response_class=HTMLResponse, summary="Fonti & salute sistema")
def sources_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = _base_context("fonti")
    context.update(
        {
            "eventi_totali": db.query(Event).count(),
            "fonti": all_source_health(db),
            "job": get_jobs_status(),
            "eta_snapshot": pipeline.cache.age_seconds,
            "limiti": [
                "Il feed JSON di Forex Factory non espone sempre il valore 'actual'. "
                "Per gli storici usare l'import CSV (Admin → calendar/import) oppure "
                "attivare il parser HTML in config/sources.yaml.",
                "Delle banche centrali viene analizzato titolo + sommario del feed RSS, "
                "non il documento integrale: e' il compromesso necessario per rispettare "
                "rate limiting e ToS.",
                "I tassi di policy sono una tabella manuale (POST /api/v1/admin/rates): "
                "se non aggiornati oltre la soglia, il Rate Differential Score perde qualita'.",
                "XAU non ha dati macro propri: lo score e' derivato come proxy inverso "
                "del dollaro e non include flussi fisici o premio geopolitico.",
                "La calibrazione richiede prezzi importati via CSV: senza prezzi gli "
                "scenari non vengono valutati ex-post.",
            ],
        }
    )
    return templates.TemplateResponse(request=request, name="fonti.html", context=context)


@router.get("/affidabilita", response_class=HTMLResponse, summary="Affidabilita'")
def reliability_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    config = get_config()
    context = _base_context("affidabilita")
    context.update(
        {
            "globale": calibration.reliability_metrics(db),
            "per_coppia": {
                pair: calibration.reliability_metrics(db, pair) for pair in config.pairs
            },
            "min_campione": calibration.MIN_SAMPLE,
        }
    )
    return templates.TemplateResponse(request=request, name="affidabilita.html", context=context)


@router.get("/storico", response_class=HTMLResponse, summary="Import storico e rigiocata")
def history_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = _base_context("storico")

    eventi = db.query(Event).count()
    primo = db.query(func.min(Event.timestamp_utc)).scalar()
    ultimo = db.query(func.max(Event.timestamp_utc)).scalar()
    con_actual = db.query(Event).filter(Event.actual.is_not(None)).count()
    rigiocati = (
        db.query(Scenario)
        .filter(Scenario.payload["origine"].as_string() == replay.ORIGINE)
        .count()
    )

    context.update(
        {
            "eventi_totali": eventi,
            "eventi_con_actual": con_actual,
            "primo_evento": primo,
            "ultimo_evento": ultimo,
            "prezzi_totali": db.query(PriceBar).count(),
            "coppie_con_prezzi": [
                row[0] for row in db.query(PriceBar.pair).distinct().all()
            ],
            "scenari_rigiocati": rigiocati,
            "scenari_valutati": db.query(ScenarioOutcome).count(),
            "min_campione": calibration.MIN_SAMPLE,
            "limiti_replay": replay.LIMITI,
        }
    )
    return templates.TemplateResponse(request=request, name="storico.html", context=context)
