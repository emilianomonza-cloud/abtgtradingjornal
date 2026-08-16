"""SEZIONE 3.2 — Motore di scoring valutario.

Ogni valuta riceve uno score composito in [-100, +100] con sei sotto-score
visibili, ognuno con motivazione, fonti e catene causali attivate.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_config
from ..storage.models import CBDocument, CurrencyScoreSnapshot, Event, PolicyRate
from ..storage.repositories import latest_documents, now_utc, recent_events
from . import taxonomy
from .causal_chains import (
    ChainActivation,
    inflation_chain_for_regime,
    merge_activations,
)
from .surprise import decay_factor

logger = logging.getLogger(__name__)

SUBSCORE_LABELS = {
    "rate_differential": "Differenziale tassi",
    "inflation_regime": "Regime inflazione",
    "growth_momentum": "Momentum crescita",
    "surprise": "Sorprese recenti",
    "cb_stance": "Stance banca centrale",
    "external_balance": "Saldo con l'estero",
}

GROWTH_CATEGORIES = (
    "GDP",
    "PMI_MANUF",
    "PMI_SERV",
    "NFP",
    "UNEMPLOYMENT",
    "JOBLESS_CLAIMS",
    "RETAIL_SALES",
    "IND_PRODUCTION",
    "CONFIDENCE",
)
INFLATION_CATEGORIES = ("CPI", "CORE_CPI", "PPI", "WAGES")
EXTERNAL_CATEGORIES = ("TRADE_BALANCE", "CURRENT_ACCOUNT")

# Peso relativo delle categorie inflazione: il core guida le decisioni di policy.
INFLATION_WEIGHTS = {"CORE_CPI": 1.0, "CPI": 0.85, "WAGES": 0.6, "PPI": 0.45}

_ANNUAL_HINTS = ("y/y", "yoy", "y-o-y", "annual", "annuale", "tendenziale")


def _clamp(value: float, low: float = -100.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


@dataclass
class SubScore:
    """Un sotto-score con la sua spiegazione (SEZIONE 3.2)."""

    name: str
    value: float
    rationale: str
    quality: float = 0.0  # 0 = nessun dato, 1 = dato completo e aggiornato
    sources: List[str] = field(default_factory=list)
    chains: List[ChainActivation] = field(default_factory=list)
    details: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.name,
            "etichetta": SUBSCORE_LABELS.get(self.name, self.name),
            "valore": round(self.value, 1),
            "motivazione": self.rationale,
            "qualita_dati": round(self.quality, 2),
            "fonti": self.sources,
            "catene": [c.chain_id for c in self.chains],
            "dettagli": self.details,
        }


@dataclass
class CurrencyEvaluation:
    """Valutazione completa di una valuta."""

    currency: str
    composite: float
    subscores: Dict[str, SubScore]
    regime: str
    data_quality: float
    computed_at: datetime
    synthetic: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def chains(self) -> List[ChainActivation]:
        out: List[ChainActivation] = []
        for sub in self.subscores.values():
            out.extend(sub.chains)
        return out

    def convergence(self) -> float:
        """Concordanza fra i sotto-score informativi: 0 = discordi, 1 = allineati.

        Misura quanto i sotto-score con dati puntano nella stessa direzione.
        Entra nel calcolo della confidenza dello scenario (SEZIONE 3.5).
        """
        values = [s.value for s in self.subscores.values() if s.quality > 0.05]
        if len(values) < 2:
            return 0.35
        mean = sum(values) / len(values)
        spread = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
        return max(0.0, min(1.0, 1.0 - spread / 60.0))

    def to_dict(self) -> Dict[str, object]:
        return {
            "valuta": self.currency,
            "score": round(self.composite, 1),
            "regime": self.regime,
            "qualita_dati": round(self.data_quality, 2),
            "convergenza": round(self.convergence(), 2),
            "sintetica": self.synthetic,
            "note": self.notes,
            "calcolato_il": self.computed_at.isoformat() + "Z",
            "sottoscore": {k: v.to_dict() for k, v in self.subscores.items()},
            "catene_attive": merge_activations(self.chains),
        }


# --------------------------------------------------------------------------- #
#  Sotto-score
# --------------------------------------------------------------------------- #


def _weighted_surprise(
    events: Sequence[Event], shrink: float = 0.75
) -> tuple[float, float, List[str]]:
    """Media pesata (per decadimento) degli impatti di sorpresa.

    Ritorna (valore, copertura, fonti). Lo shrink al denominatore evita che
    un singolo evento produca da solo uno score massimo.
    """
    numerator = 0.0
    denominator = shrink
    sources: List[str] = []
    for event in events:
        if event.surprise_impact is None:
            continue
        weight = decay_factor(event.timestamp_utc, taxonomy.category_family(event.category))
        if weight < 0.02:
            continue
        numerator += event.surprise_impact * weight
        denominator += weight
        if len(sources) < 6:
            sources.append(f"{event.title} ({event.timestamp_utc:%d/%m}, {event.surprise_label})")
    if denominator <= shrink:
        return 0.0, 0.0, sources
    coverage = min(1.0, (denominator - shrink) / 1.5)
    return _clamp(numerator / denominator), coverage, sources


def score_rate_differential(session: Session, currency: str, all_rates: Dict[str, PolicyRate]) -> SubScore:
    config = get_config()
    staleness_days = int(
        (config.sources.get("tassi_policy", {}) or {}).get("giorni_staleness", 45)
    )
    own = all_rates.get(currency)
    if own is None:
        return SubScore(
            name="rate_differential",
            value=0.0,
            rationale="Tasso di policy non disponibile: sotto-score neutro.",
            quality=0.0,
        )

    others = [r.rate for c, r in all_rates.items() if c != currency]
    peer_avg = sum(others) / len(others) if others else own.rate
    differential = own.rate - peer_avg
    base = 100.0 * math.tanh(differential / 2.0)

    decisions = recent_events(
        session, currency, int(config.weights.get("lookback_giorni", 90)), ["RATE_DECISION"]
    )
    momentum, coverage, sources = _weighted_surprise(decisions, shrink=1.0)
    value = _clamp(0.7 * base + 0.3 * momentum)

    age_days = (now_utc() - (own.updated_at or now_utc())).days
    quality = 1.0 if age_days <= staleness_days else 0.55
    if own.effective_date is not None:
        eff_age = (now_utc() - own.effective_date).days
        if eff_age > 400:
            quality = min(quality, 0.5)

    rationale = (
        f"Tasso {own.central_bank} {own.rate:.2f}% contro media pari {peer_avg:.2f}% "
        f"(differenziale {differential:+.2f}pp). "
        + (
            f"Sorprese di policy recenti: impulso {momentum:+.0f}."
            if coverage > 0
            else "Nessuna decisione di tasso recente nel campione."
        )
    )
    chains = [
        ChainActivation(
            chain_id="TASSI_CAPITALI_CAMBIO",
            direction=1 if value >= 0 else -1,
            strength=min(1.0, abs(value) / 100.0),
            trigger=f"Differenziale tassi {differential:+.2f}pp",
            currency=currency,
            sources=[f"{own.central_bank} {own.rate:.2f}% ({own.source})"],
        )
    ]
    return SubScore(
        name="rate_differential",
        value=value,
        rationale=rationale,
        quality=quality,
        sources=[f"Tasso policy: {own.source}"] + sources,
        chains=chains,
        details={
            "tasso": own.rate,
            "media_pari": round(peer_avg, 3),
            "differenziale": round(differential, 3),
            "aggiornato": own.updated_at.isoformat() if own.updated_at else None,
        },
    )


def _is_annual(event: Event) -> bool:
    title = (event.title or "").lower()
    return any(hint in title for hint in _ANNUAL_HINTS)


def score_inflation_regime(session: Session, currency: str, regime: str) -> SubScore:
    config = get_config()
    lookback = int(config.weights.get("lookback_giorni", 90))
    events = recent_events(session, currency, lookback, INFLATION_CATEGORIES)
    if not events:
        return SubScore(
            name="inflation_regime",
            value=0.0,
            rationale="Nessun dato di inflazione nel periodo di look-back.",
            quality=0.0,
        )

    targets = config.weights.get("target_inflazione", {}) or {}
    target = float(targets.get(currency, 2.0))

    # Componente 'livello': distanza dal target sull'ultimo dato tendenziale.
    gap_score = 0.0
    gap_source: Optional[str] = None
    gap_value: Optional[float] = None
    for event in events:
        if event.category not in ("CORE_CPI", "CPI"):
            continue
        if not _is_annual(event) or event.actual is None:
            continue
        gap_value = event.actual - target
        gap_score = 100.0 * math.tanh(gap_value / 1.5)
        gap_source = f"{event.title} {event.actual:g}% vs target {target:g}%"
        break

    # Componente 'sorpresa', pesata per rilevanza della categoria.
    numerator = 0.0
    denominator = 0.75
    sources: List[str] = []
    for event in events:
        if event.surprise_impact is None:
            continue
        weight = decay_factor(
            event.timestamp_utc, taxonomy.category_family(event.category)
        ) * INFLATION_WEIGHTS.get(event.category, 0.5)
        if weight < 0.02:
            continue
        numerator += event.surprise_impact * weight
        denominator += weight
        if len(sources) < 5:
            sources.append(f"{event.title} ({event.surprise_label})")
    surprise_component = _clamp(numerator / denominator) if denominator > 0.75 else 0.0

    if gap_source is not None:
        raw = 0.55 * gap_score + 0.45 * surprise_component
        quality = 0.95
    else:
        raw = surprise_component
        quality = 0.6 if sources else 0.2

    # Il regime decide QUALE canale domina (SEZIONE 3.1, catena inflazione).
    regime_sign = -1.0 if str(regime).upper() == "CREDIBILITY_LOSS" else 1.0
    value = _clamp(raw * regime_sign)

    chain_id = inflation_chain_for_regime(regime)
    chains = [
        ChainActivation(
            chain_id=chain_id,
            direction=1 if raw >= 0 else -1,
            strength=min(1.0, abs(raw) / 100.0),
            trigger=gap_source or "Sorprese sui prezzi",
            currency=currency,
            sources=sources[:3],
        )
    ]
    if abs(value) > 25:
        chains.append(
            ChainActivation(
                chain_id="MONETARIA_RESTRITTIVA" if value > 0 else "MONETARIA_ESPANSIVA",
                direction=1,
                strength=min(1.0, abs(value) / 100.0) * 0.7,
                trigger="Attese di reazione della banca centrale all'inflazione",
                currency=currency,
                sources=sources[:2],
            )
        )

    parts = []
    if gap_source:
        parts.append(gap_source)
    if sources:
        parts.append(f"sorprese: {surprise_component:+.0f}")
    parts.append(f"regime {regime}")
    rationale = ". ".join(parts) + "."

    return SubScore(
        name="inflation_regime",
        value=value,
        rationale=rationale[:280],
        quality=quality,
        sources=sources,
        chains=chains,
        details={
            "target": target,
            "gap": round(gap_value, 2) if gap_value is not None else None,
            "componente_livello": round(gap_score, 1),
            "componente_sorpresa": round(surprise_component, 1),
            "regime": regime,
        },
    )


def score_growth_momentum(session: Session, currency: str) -> SubScore:
    config = get_config()
    lookback = int(config.weights.get("lookback_giorni", 90))
    events = recent_events(session, currency, lookback, GROWTH_CATEGORIES)
    if not events:
        return SubScore(
            name="growth_momentum",
            value=0.0,
            rationale="Nessun dato di crescita/occupazione nel periodo di look-back.",
            quality=0.0,
        )

    surprise_component, coverage, sources = _weighted_surprise(events)

    # Componente 'livello' dai PMI: la distanza dalla soglia 50 e' informazione
    # autonoma rispetto alla sorpresa (espansione vs contrazione).
    level_component: Optional[float] = None
    level_source: Optional[str] = None
    for event in events:
        neutral = taxonomy.category_neutral_level(event.category)
        if neutral is None or event.actual is None:
            continue
        level_component = 100.0 * math.tanh((event.actual - neutral) / 3.0)
        level_source = f"{event.title} {event.actual:g} (soglia {neutral:g})"
        break

    if level_component is not None:
        value = _clamp(0.6 * surprise_component + 0.4 * level_component)
        quality = min(1.0, 0.55 + 0.45 * coverage)
    else:
        value = surprise_component
        quality = min(0.85, 0.25 + 0.6 * coverage)

    chains = [
        ChainActivation(
            chain_id="PIL_CRESCITA",
            direction=1 if value >= 0 else -1,
            strength=min(1.0, abs(value) / 100.0),
            trigger=level_source or (sources[0] if sources else "Dati di attivita' recenti"),
            currency=currency,
            sources=sources[:3],
        )
    ]

    rationale = (
        (f"{level_source}. " if level_source else "")
        + f"Impulso da {len(sources)} rilasci recenti: {surprise_component:+.0f}."
    )
    return SubScore(
        name="growth_momentum",
        value=value,
        rationale=rationale[:280],
        quality=quality,
        sources=sources,
        chains=chains,
        details={
            "componente_sorpresa": round(surprise_component, 1),
            "componente_livello": round(level_component, 1) if level_component is not None else None,
            "eventi_considerati": len(events),
        },
    )


def score_surprise(session: Session, currency: str) -> SubScore:
    config = get_config()
    lookback = int(config.weights.get("lookback_giorni", 90))
    events = recent_events(session, currency, lookback)
    events = [e for e in events if e.surprise_impact not in (None, 0.0)]
    if not events:
        return SubScore(
            name="surprise",
            value=0.0,
            rationale="Nessuna sorpresa rilevante nel periodo di look-back.",
            quality=0.0,
        )

    value, coverage, sources = _weighted_surprise(events)
    big = [e for e in events if e.surprise_label in ("BIG_BEAT", "BIG_MISS")]
    chains: List[ChainActivation] = []
    for event in events[:6]:
        chain_id = taxonomy.category_spec(event.category).get("catena")
        if not chain_id or event.surprise_impact is None:
            continue
        weight = decay_factor(event.timestamp_utc, taxonomy.category_family(event.category))
        if weight < 0.1:
            continue
        chains.append(
            ChainActivation(
                chain_id=str(chain_id),
                direction=1 if event.surprise_impact >= 0 else -1,
                strength=min(1.0, abs(event.surprise_impact) / 100.0) * weight,
                trigger=f"{event.title}: {event.surprise_label}",
                currency=currency,
                sources=[event.source],
            )
        )

    rationale = (
        f"{len(events)} rilasci con sorpresa nel campione"
        + (f", di cui {len(big)} estreme" if big else "")
        + f". Impulso netto ponderato per decadimento: {value:+.0f}."
    )
    return SubScore(
        name="surprise",
        value=value,
        rationale=rationale,
        quality=min(1.0, 0.3 + 0.7 * coverage),
        sources=sources,
        chains=chains,
        details={"eventi": len(events), "estremi": len(big)},
    )


def score_cb_stance(session: Session, currency: str) -> SubScore:
    config = get_config()
    lookback = int(config.weights.get("lookback_giorni", 90))
    documents = latest_documents(session, currency, lookback_days=lookback, limit=25)
    documents = [d for d in documents if abs(d.tone_score) > 1e-6]
    if not documents:
        return SubScore(
            name="cb_stance",
            value=0.0,
            rationale="Nessun comunicato o discorso analizzato nel periodo.",
            quality=0.0,
        )

    numerator = 0.0
    denominator = 0.5
    sources: List[str] = []
    for doc in documents:
        weight = decay_factor(doc.published_at, "policy")
        if doc.doc_type == "PRESS_RELEASE":
            weight *= 1.3
        elif doc.doc_type == "MINUTES":
            weight *= 1.1
        if weight < 0.03:
            continue
        numerator += doc.tone_score * weight
        denominator += weight
        if len(sources) < 5:
            sources.append(f"{doc.bank}: {doc.title[:70]} ({doc.tone_label})")

    value = _clamp(numerator / denominator) if denominator > 0.5 else 0.0
    coverage = min(1.0, (denominator - 0.5) / 2.0)

    chains = [
        ChainActivation(
            chain_id="TASSI_CAPITALI_CAMBIO",
            direction=1 if value >= 0 else -1,
            strength=min(1.0, abs(value) / 100.0) * 0.8,
            trigger=f"Tono aggregato dei comunicati: {value:+.0f}",
            currency=currency,
            sources=sources[:3],
        ),
        ChainActivation(
            chain_id="MONETARIA_RESTRITTIVA" if value >= 0 else "MONETARIA_ESPANSIVA",
            direction=1,
            strength=min(1.0, abs(value) / 100.0),
            trigger="Guidance della banca centrale",
            currency=currency,
            sources=sources[:2],
        ),
    ]
    label = "hawkish" if value > 20 else ("dovish" if value < -20 else "neutrale")
    return SubScore(
        name="cb_stance",
        value=value,
        rationale=f"{len(documents)} documenti analizzati, tono aggregato {label} ({value:+.0f}).",
        quality=min(1.0, 0.35 + 0.65 * coverage),
        sources=sources,
        chains=chains,
        details={"documenti": len(documents), "tono": round(value, 1)},
    )


def score_external_balance(session: Session, currency: str) -> SubScore:
    config = get_config()
    lookback = max(120, int(config.weights.get("lookback_giorni", 90)))
    events = recent_events(session, currency, lookback, EXTERNAL_CATEGORIES)
    if not events:
        return SubScore(
            name="external_balance",
            value=0.0,
            rationale="Nessun dato su bilancia commerciale o partite correnti.",
            quality=0.0,
        )

    surprise_component, coverage, sources = _weighted_surprise(events, shrink=0.5)

    latest = events[0]
    level_component = 0.0
    if latest.actual is not None:
        level_component = 30.0 if latest.actual > 0 else -30.0

    value = _clamp(0.6 * surprise_component + 0.4 * level_component)
    chain_id = "BILANCIA_PAGAMENTI" if latest.category == "CURRENT_ACCOUNT" else "COMMERCIO_ESTERO"
    chains = [
        ChainActivation(
            chain_id=chain_id,
            direction=1 if value >= 0 else -1,
            strength=min(1.0, abs(value) / 100.0) * 0.6,
            trigger=f"{latest.title}: {latest.actual_raw or latest.actual}",
            currency=currency,
            sources=[latest.source],
        )
    ]
    saldo = "avanzo" if level_component > 0 else "disavanzo"
    return SubScore(
        name="external_balance",
        value=value,
        rationale=f"Ultimo saldo in {saldo} ({latest.actual_raw or latest.actual}). Canale lento, peso ridotto.",
        quality=min(0.9, 0.4 + 0.5 * coverage),
        sources=sources,
        chains=chains,
        details={"ultimo_saldo": latest.actual, "categoria": latest.category},
    )


# --------------------------------------------------------------------------- #
#  Composizione
# --------------------------------------------------------------------------- #


def evaluate_currency(
    session: Session,
    currency: str,
    rates: Optional[Dict[str, PolicyRate]] = None,
) -> CurrencyEvaluation:
    """Calcola lo score composito di una valuta reale.

    `rates` permette di passare una fotografia dei tassi diversa da quella in
    archivio: serve alla rigiocata storica, dove i tassi di oggi sarebbero un
    dato del futuro rispetto alla data simulata.
    """
    config = get_config()
    currency = currency.upper()
    regime = config.regime_for(currency)

    if rates is None:
        rates = {
            r.currency: r
            for r in session.execute(select(PolicyRate)).scalars().all()
            if r.currency in config.currencies
        }

    subscores = {
        "rate_differential": score_rate_differential(session, currency, rates),
        "inflation_regime": score_inflation_regime(session, currency, regime),
        "growth_momentum": score_growth_momentum(session, currency),
        "surprise": score_surprise(session, currency),
        "cb_stance": score_cb_stance(session, currency),
        "external_balance": score_external_balance(session, currency),
    }

    weights = config.subscore_weights
    composite = 0.0
    quality = 0.0
    for name, sub in subscores.items():
        weight = float(weights.get(name, 0.0))
        composite += weight * sub.value
        quality += weight * sub.quality

    notes: List[str] = []
    missing = [SUBSCORE_LABELS[n] for n, s in subscores.items() if s.quality <= 0.05]
    if missing:
        notes.append(
            "Sotto-score senza dati (contribuiscono come neutri e abbassano la confidenza): "
            + ", ".join(missing)
        )

    return CurrencyEvaluation(
        currency=currency,
        composite=_clamp(composite),
        subscores=subscores,
        regime=regime,
        data_quality=max(0.0, min(1.0, quality)),
        computed_at=now_utc(),
        notes=notes,
    )


def evaluate_synthetic(
    currency: str, spec: Dict[str, object], base_eval: Optional[CurrencyEvaluation]
) -> CurrencyEvaluation:
    """Valuta una pseudo-valuta (XAU) come funzione inversa di una valuta reale."""
    factor = float(spec.get("fattore", 0.6))
    reference = str(spec.get("inverso_di", "USD")).upper()
    note = str(spec.get("nota", ""))

    if base_eval is None:
        return CurrencyEvaluation(
            currency=currency,
            composite=0.0,
            subscores={},
            regime="N/D",
            data_quality=0.0,
            computed_at=now_utc(),
            synthetic=True,
            notes=[f"Valuta di riferimento {reference} non valutata: score neutro.", note],
        )

    value = _clamp(-factor * base_eval.composite)
    proxy = SubScore(
        name="rate_differential",
        value=value,
        rationale=(
            f"Derivato: {factor:g} × score {reference} invertito ({base_eval.composite:+.0f}). "
            "Proxy dei tassi reali, non un modello proprio dell'oro."
        ),
        quality=base_eval.data_quality * 0.7,
        sources=[f"score {reference}"],
        chains=[
            ChainActivation(
                chain_id="TASSI_CAPITALI_CAMBIO",
                direction=1 if value >= 0 else -1,
                strength=min(1.0, abs(value) / 100.0) * 0.7,
                trigger=f"Costo opportunita' via {reference}",
                currency=currency,
                sources=[reference],
            )
        ],
        details={"riferimento": reference, "fattore": factor},
    )
    return CurrencyEvaluation(
        currency=currency,
        composite=value,
        subscores={"rate_differential": proxy},
        regime=base_eval.regime,
        data_quality=base_eval.data_quality * 0.7,
        computed_at=now_utc(),
        synthetic=True,
        notes=[note] if note else [],
    )


def evaluate_all(
    session: Session, rates: Optional[Dict[str, PolicyRate]] = None
) -> Dict[str, CurrencyEvaluation]:
    """Valuta tutte le valute configurate (reali + sintetiche)."""
    config = get_config()
    results: Dict[str, CurrencyEvaluation] = {}
    for currency in config.currencies:
        try:
            results[currency] = evaluate_currency(session, currency, rates)
        except Exception:  # pragma: no cover - difensivo
            logger.exception("Errore nel calcolo dello score per %s", currency)
    for currency, spec in config.synthetic.items():
        reference = str((spec or {}).get("inverso_di", "USD")).upper()
        results[currency.upper()] = evaluate_synthetic(
            currency.upper(), spec or {}, results.get(reference)
        )
    return results


def persist_scores(session: Session, evaluations: Dict[str, CurrencyEvaluation]) -> int:
    """Storicizza gli score per la sezione 'variazione 24h' e la calibrazione."""
    count = 0
    for evaluation in evaluations.values():
        session.add(
            CurrencyScoreSnapshot(
                currency=evaluation.currency,
                computed_at=evaluation.computed_at,
                composite=evaluation.composite,
                subscores={k: v.to_dict() for k, v in evaluation.subscores.items()},
                chains=merge_activations(evaluation.chains),
                data_quality=evaluation.data_quality,
                regime=evaluation.regime,
            )
        )
        count += 1
    return count


def score_change_24h(session: Session, currency: str, current: float) -> Optional[float]:
    """Variazione dello score rispetto al valore piu' vicino a 24 ore fa."""
    target = now_utc() - timedelta(hours=24)
    row = session.execute(
        select(CurrencyScoreSnapshot)
        .where(
            CurrencyScoreSnapshot.currency == currency.upper(),
            CurrencyScoreSnapshot.computed_at <= target,
        )
        .order_by(CurrencyScoreSnapshot.computed_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    return current - row.composite
