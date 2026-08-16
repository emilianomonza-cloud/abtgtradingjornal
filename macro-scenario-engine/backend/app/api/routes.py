"""SEZIONE 5 — Endpoint pubblici JSON."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import pipeline
from ..calibration import evaluation as calibration
from ..config import get_config, get_settings
from ..engine import causal_chains, playbooks
from ..engine.surprise import compute_surprise
from ..storage.models import PolicyRate
from ..storage.repositories import (
    all_source_health,
    events_between,
    latest_documents,
    now_utc,
)
from .deps import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["macro-scenario-engine"])

DISCLAIMER = (
    "Scenari probabilistici generati da modello. Non costituiscono consulenza "
    "finanziaria. La probabilita' e' una stima soggetta a errore."
)


@router.get("/health", summary="Stato del sistema e delle fonti")
def health(db: Session = Depends(get_db)) -> Dict[str, Any]:
    sources = [h.to_dict() for h in all_source_health(db)]
    ok = sum(1 for s in sources if s["status"] == "OK")
    age = pipeline.cache.age_seconds
    return {
        "stato": "OK" if not sources or ok == len(sources) else "DEGRADED",
        "ora_server_utc": now_utc().isoformat() + "Z",
        "fuso_orario": get_config().operator.get("fuso_orario"),
        "snapshot_eta_secondi": round(age, 1) if age is not None else None,
        "layer_llm": "attivo" if get_settings().llm_enabled else "rule-based",
        "fonti": sources,
        "fonti_ok": ok,
        "fonti_totali": len(sources),
        "disclaimer": DISCLAIMER,
    }


@router.get("/currencies/scores", summary="Score composito di tutte le valute")
def currency_scores(db: Session = Depends(get_db)) -> Dict[str, Any]:
    pipeline.ensure_fresh(db)
    scores = pipeline.cache.scores
    return {
        "aggiornato_il": (
            pipeline.cache.updated_at.isoformat() + "Z"
            if pipeline.cache.updated_at
            else None
        ),
        "pesi": get_config().subscore_weights,
        "valute": scores,
        "disclaimer": DISCLAIMER,
    }


@router.get("/currencies/{currency}/score", summary="Dettaglio di una singola valuta")
def currency_score(currency: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    pipeline.ensure_fresh(db)
    data = pipeline.cache.scores.get(currency.upper())
    if data is None:
        raise HTTPException(status_code=404, detail=f"Valuta non configurata: {currency}")
    return data


@router.get("/pairs", summary="Elenco delle coppie configurate")
def pairs(db: Session = Depends(get_db)) -> Dict[str, Any]:
    pipeline.ensure_fresh(db)
    out = []
    for pair, payload in pipeline.cache.scenarios.items():
        base = payload["scenari"][0]
        out.append(
            {
                "coppia": pair,
                "bias": payload["bias"],
                "bias_label": payload["bias_label"],
                "confidenza": payload["confidenza"],
                "scenario_base": {
                    "direzione": base["direzione"],
                    "probabilita": base["probabilita"],
                },
                "scade_il": payload["scade_il"],
                "riscrivibile": payload["riscrivibile"],
            }
        )
    return {"coppie": out, "disclaimer": DISCLAIMER}


@router.get("/pairs/{symbol}/scenarios", summary="Scenari probabilistici di una coppia")
def pair_scenarios(
    symbol: str,
    horizon: Optional[str] = Query(
        None, description="intraday | h24_72 | w1_4 (default: da configurazione)"
    ),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    config = get_config()
    pair = symbol.upper()
    if pair not in config.pairs:
        raise HTTPException(status_code=404, detail=f"Coppia non configurata: {symbol}")
    if horizon and horizon not in config.horizons:
        raise HTTPException(status_code=400, detail=f"Orizzonte sconosciuto: {horizon}")

    pipeline.ensure_fresh(db)
    if horizon is None or horizon == config.default_horizon:
        cached = pipeline.cache.scenarios.get(pair)
        if cached is not None:
            return cached

    # Orizzonte diverso da quello di default: generazione su richiesta,
    # senza storicizzare (evita di inquinare il campione di calibrazione).
    from ..engine import scoring

    evaluations = scoring.evaluate_all(db)
    from ..engine import scenarios as scenario_engine

    return scenario_engine.generate_pair_scenarios(db, pair, evaluations, horizon)


@router.get("/calendar", summary="Calendario economico normalizzato")
def calendar(
    from_: Optional[datetime] = Query(None, alias="from"),
    to: Optional[datetime] = Query(None),
    impact: Optional[str] = Query(None, description="RED, ORANGE, YELLOW (separati da virgola)"),
    currency: Optional[str] = Query(None, description="Valute separate da virgola"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    start = from_ or (now_utc() - timedelta(days=2))
    end = to or (now_utc() + timedelta(days=7))
    impacts = [i.strip().upper() for i in impact.split(",")] if impact else None
    currencies = [c.strip().upper() for c in currency.split(",")] if currency else None

    events = events_between(db, start, end, currencies, impacts)
    payload: List[Dict[str, Any]] = []
    for event in events:
        book = playbooks.playbook_for(event.category, event.currency)
        item = event.to_dict()
        item["playbook"] = {
            "nome": book.get("nome"),
            "catena_attesa": book.get("catena_attesa"),
            "volatilita_attesa": book.get("volatilita_attesa"),
            "no_trade_minuti": playbooks.window_for_event(event),
            "eccezioni": book.get("eccezioni", []),
            "second_order": book.get("second_order", []),
        }
        if event.actual is not None:
            item["sorpresa"] = compute_surprise(db, event).to_dict()
        payload.append(item)

    return {"da": start.isoformat(), "a": end.isoformat(), "eventi": payload}


@router.get("/calendar/next-red", summary="Prossimi eventi RED per le valute configurate")
def next_red(
    limit: int = Query(3, ge=1, le=20), db: Session = Depends(get_db)
) -> Dict[str, Any]:
    config = get_config()
    events = events_between(
        db, now_utc(), now_utc() + timedelta(days=14), config.currencies, ["RED"]
    )
    now = now_utc()
    out = [
        {
            **event.to_dict(),
            "minuti_mancanti": int((event.timestamp_utc - now).total_seconds() // 60),
            "finestra_no_trade": playbooks.window_for_event(event),
        }
        for event in events[:limit]
    ]
    return {"eventi": out}


@router.get("/central-banks", summary="Stance delle banche centrali")
def central_banks(db: Session = Depends(get_db)) -> Dict[str, Any]:
    config = get_config()
    pipeline.ensure_fresh(db)
    rates = {r.currency: r for r in db.query(PolicyRate).all()}

    out = []
    for currency, bank in config.central_banks.items():
        documents = latest_documents(db, currency, lookback_days=180, limit=5)
        score = pipeline.cache.scores.get(currency, {})
        stance = (score.get("sottoscore", {}) or {}).get("cb_stance", {})
        rate = rates.get(currency)
        out.append(
            {
                "banca": bank,
                "valuta": currency,
                "tasso_policy": rate.to_dict() if rate else None,
                "stance_score": stance.get("valore"),
                "stance_motivazione": stance.get("motivazione"),
                "ultimo_documento": documents[0].to_dict() if documents else None,
                "documenti_recenti": [d.to_dict() for d in documents],
            }
        )
    return {"banche": out, "disclaimer": DISCLAIMER}


@router.get("/sources", summary="Stato dei collector")
def sources(db: Session = Depends(get_db)) -> Dict[str, Any]:
    return {
        "fonti": [h.to_dict() for h in all_source_health(db)],
        "limiti_dichiarati": [
            "Il feed JSON di Forex Factory non espone sempre il valore 'actual': "
            "per gli storici usare l'import CSV o attivare il parser HTML.",
            "Dei documenti delle banche centrali viene analizzato titolo + sommario "
            "del feed RSS, non il testo integrale (rate limiting e ToS).",
            "I tassi di policy sono una tabella manuale: verificarne la data di "
            "aggiornamento prima di operare.",
            "L'oro (XAU) non ha dati macro propri: lo score e' derivato come proxy "
            "inverso del dollaro.",
        ],
    }


@router.get("/chains", summary="Catene causali del modello (SEZIONE 3.1)")
def chains() -> Dict[str, Any]:
    return {"catene": causal_chains.all_chains()}


@router.get("/playbooks", summary="Playbook eventi (SEZIONE 8)")
def playbook_list() -> Dict[str, Any]:
    return playbooks.all_playbooks()


@router.get("/reliability", summary="Affidabilita' storica degli scenari (SEZIONE 7)")
def reliability(
    pair: Optional[str] = Query(None), db: Session = Depends(get_db)
) -> Dict[str, Any]:
    return calibration.reliability_metrics(db, pair)


@router.get("/mt5/snapshot", summary="Payload compatto per il bridge MQL5")
def mt5_snapshot(db: Session = Depends(get_db)) -> Dict[str, Any]:
    snapshot = pipeline.cache.mt5
    if not snapshot:
        pipeline.refresh(db)
        snapshot = pipeline.cache.mt5
    return snapshot
