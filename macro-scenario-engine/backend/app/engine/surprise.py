"""SEZIONE 3.3 — Gestione delle sorprese (actual vs forecast vs previous)."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from ..config import get_config
from ..storage.models import Event
from ..storage.repositories import historical_series, now_utc
from . import taxonomy

BIG_BEAT = "BIG_BEAT"
BEAT = "BEAT"
IN_LINE = "IN_LINE"
MISS = "MISS"
BIG_MISS = "BIG_MISS"
NOT_RELEASED = "NON_RILASCIATO"

# Numero minimo di rilasci storici per stimare sigma dai dati invece che dal default
MIN_HISTORY = 6


@dataclass
class SurpriseResult:
    """Esito della classificazione di una sorpresa."""

    label: str
    z: float
    impact: float  # contributo direzionale sulla valuta, [-100, +100]
    sigma: float
    sigma_source: str
    reference: str  # 'forecast' | 'previous' | 'nessuno'
    delta: Optional[float] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "classificazione": self.label,
            "z": round(self.z, 2),
            "impatto": round(self.impact, 1),
            "sigma": round(self.sigma, 4),
            "sigma_fonte": self.sigma_source,
            "riferimento": self.reference,
            "delta": round(self.delta, 4) if self.delta is not None else None,
            "note": self.notes,
        }


def estimate_sigma(
    session: Session,
    currency: str,
    category: str,
    exclude_event_id: Optional[int] = None,
) -> tuple[float, str]:
    """Deviazione tipica di (actual − forecast) per l'indicatore.

    Usa lo storico quando disponibile (>= MIN_HISTORY rilasci), altrimenti la
    sigma dichiarata in indicators.yaml. Nessuna stima inventata su 2 dati.

    L'evento che si sta valutando viene ESCLUSO dal proprio campione: se
    entrasse nella stima, una sorpresa estrema gonfierebbe la sigma e
    attenuerebbe se stessa.
    """
    default_sigma = taxonomy.category_sigma(category)
    history = historical_series(session, currency, category, limit=40)
    deltas = [
        e.actual - e.forecast
        for e in history
        if e.actual is not None
        and e.forecast is not None
        and (exclude_event_id is None or e.id != exclude_event_id)
    ]
    if len(deltas) >= MIN_HISTORY:
        try:
            sigma = statistics.pstdev(deltas)
        except statistics.StatisticsError:  # pragma: no cover - difensivo
            sigma = 0.0
        if sigma > 1e-9:
            return sigma, f"storico ({len(deltas)} rilasci)"
    return max(default_sigma, 1e-9), "default indicators.yaml"


def classify(z: float) -> str:
    thresholds = get_config().weights.get("soglie_sorpresa", {}) or {}
    big = float(thresholds.get("big", 2.0))
    normal = float(thresholds.get("normale", 0.5))
    if z >= big:
        return BIG_BEAT
    if z >= normal:
        return BEAT
    if z <= -big:
        return BIG_MISS
    if z <= -normal:
        return MISS
    return IN_LINE


def compute_surprise(session: Session, event: Event) -> SurpriseResult:
    """Calcola la sorpresa di un evento gia' rilasciato.

    Il segno di `z` e' gia' orientato in senso VALUTARIO: z > 0 significa
    'dato favorevole alla valuta' anche per gli indicatori invertiti
    (disoccupazione, sussidi), che vengono ribaltati tramite `direzione`.
    """
    notes: List[str] = []
    category = event.category or "OTHER"

    if event.actual is None:
        return SurpriseResult(
            label=NOT_RELEASED,
            z=0.0,
            impact=0.0,
            sigma=0.0,
            sigma_source="n/d",
            reference="nessuno",
            notes=["Dato non ancora pubblicato."],
        )

    if taxonomy.category_subscore(category) is None:
        return SurpriseResult(
            label=IN_LINE,
            z=0.0,
            impact=0.0,
            sigma=0.0,
            sigma_source="n/d",
            reference="nessuno",
            notes=["Indicatore non modellato: archiviato ma non usato negli score."],
        )

    if event.forecast is not None:
        reference_value = event.forecast
        reference = "forecast"
    elif event.previous is not None:
        reference_value = event.previous
        reference = "previous"
        notes.append("Forecast assente: confronto con il dato precedente, affidabilita' ridotta.")
    else:
        return SurpriseResult(
            label=IN_LINE,
            z=0.0,
            impact=0.0,
            sigma=0.0,
            sigma_source="n/d",
            reference="nessuno",
            notes=["Ne' forecast ne' previous disponibili: sorpresa non calcolabile."],
        )

    sigma, sigma_source = estimate_sigma(
        session, event.currency, category, exclude_event_id=event.id
    )
    direction = taxonomy.category_direction(category)
    delta = event.actual - reference_value
    z = (delta / sigma) * direction
    # Limite prudenziale: sorprese oltre 6 sigma sono quasi sempre errori di
    # unita' di misura della fonte, non informazione economica.
    if abs(z) > 6.0:
        notes.append(
            f"Sorpresa oltre 6 sigma (z={z:.1f}): valore troncato, possibile disallineamento di unita'."
        )
        z = math.copysign(6.0, z)

    label = classify(z)

    # Impatto direzionale saturato: nessuna sorpresa produce da sola il massimo score.
    impact = 100.0 * math.tanh(z / 2.5)

    # Eccezione PMI/ISM: superare il forecast restando sotto la soglia di
    # espansione (50) non e' un segnale di espansione. Impulso dimezzato.
    neutral = taxonomy.category_neutral_level(category)
    if neutral is not None and event.actual is not None:
        above_neutral = event.actual >= neutral
        if (z > 0 and not above_neutral) or (z < 0 and above_neutral):
            impact *= 0.5
            notes.append(
                f"Sorpresa discorde dal livello neutro ({neutral:g}): impulso dimezzato."
            )

    # Revisione del dato precedente in direzione opposta: attenua la sorpresa.
    if event.revised is not None and event.previous is not None:
        revision = (event.revised - event.previous) * direction
        if revision * z < 0 and abs(revision) > 0.25 * sigma:
            impact *= 0.7
            notes.append(
                "Revisione del dato precedente in direzione opposta: impulso ridotto del 30%."
            )

    weights = get_config().weights.get("peso_impatto", {}) or {}
    impact *= float(weights.get(event.impact, 0.5))

    return SurpriseResult(
        label=label,
        z=z,
        impact=impact,
        sigma=sigma,
        sigma_source=sigma_source,
        reference=reference,
        delta=delta,
        notes=notes,
    )


def decay_factor(event_time: datetime, family: str, reference: Optional[datetime] = None) -> float:
    """Decadimento esponenziale dell'impatto di un evento (half-life configurabile)."""
    config = get_config()
    half_lives = config.weights.get("half_life_giorni", {}) or {}
    half_life = float(half_lives.get(family, half_lives.get("ciclici", 5)))
    if half_life <= 0:
        return 1.0
    reference = reference or now_utc()
    if event_time.tzinfo is not None:
        event_time = event_time.replace(tzinfo=None)
    age_days = max(0.0, (reference - event_time).total_seconds() / 86400.0)
    return 0.5 ** (age_days / half_life)


def apply_and_store(session: Session, event: Event) -> SurpriseResult:
    """Calcola la sorpresa e la persiste sull'evento."""
    result = compute_surprise(session, event)
    event.surprise_label = result.label
    event.surprise_z = result.z
    event.surprise_impact = result.impact
    return result
