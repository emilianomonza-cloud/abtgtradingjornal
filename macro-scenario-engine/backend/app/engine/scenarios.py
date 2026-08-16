"""SEZIONE 3.5 — Generatore di scenari probabilistici.

Per ogni coppia vengono SEMPRE prodotti tre scenari (base + due alternativi) con:
probabilita' che sommano a 100%, confidenza qualitativa, catene causali attive,
driver, condizioni di invalidazione, eventi che possono riscrivere lo scenario,
suggerimento operativo non vincolante, timestamp e scadenza.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from ..config import get_config
from ..storage.models import Event, Scenario
from ..storage.repositories import all_source_health, events_between, now_utc
from . import taxonomy
from .causal_chains import ChainActivation, merge_activations
from .playbooks import playbook_for, no_trade_window
from .scoring import SUBSCORE_LABELS, CurrencyEvaluation

logger = logging.getLogger(__name__)

RIALZO = "RIALZO"
RIBASSO = "RIBASSO"
LATERALE = "LATERALE"

ALTA = "ALTA"
MEDIA = "MEDIA"
BASSA = "BASSA"
_CONFIDENCE_ORDER = [BASSA, MEDIA, ALTA]

# Propensione di ciascuna valuta a rafforzarsi in fase di avversione al rischio.
SAFE_HAVEN_RANK = {
    "USD": 2.0,
    "JPY": 3.0,
    "CHF": 3.0,
    "XAU": 2.5,
    "EUR": 1.0,
    "GBP": 0.0,
    "CAD": -0.5,
    "AUD": -1.5,
    "NZD": -1.5,
}


def _downgrade(level: str, steps: int = 1) -> str:
    index = _CONFIDENCE_ORDER.index(level) if level in _CONFIDENCE_ORDER else 1
    return _CONFIDENCE_ORDER[max(0, index - steps)]


def _direction_from_bias(bias: float, neutral_threshold: float) -> str:
    if bias > neutral_threshold:
        return RIALZO
    if bias < -neutral_threshold:
        return RIBASSO
    return LATERALE


def _magnitude(bias_abs: float, event_risk: float) -> str:
    score = bias_abs / 100.0 + event_risk * 0.5
    if score >= 0.7:
        return "ampia"
    if score >= 0.35:
        return "moderata"
    return "contenuta"


@dataclass
class PairContext:
    """Tutto cio' che serve per generare gli scenari di una coppia."""

    pair: str
    base: str
    quote: str
    base_eval: CurrencyEvaluation
    quote_eval: CurrencyEvaluation
    bias: float
    bias_label: str
    horizon: str
    horizon_hours: int
    events: List[Event] = field(default_factory=list)
    future_events: List[Event] = field(default_factory=list)
    red_events: List[Event] = field(default_factory=list)
    next_red: Optional[Event] = None
    event_risk: float = 0.0
    source_ratio: float = 1.0


# --------------------------------------------------------------------------- #
#  Contesto
# --------------------------------------------------------------------------- #


def classify_bias(bias: float) -> str:
    thresholds = get_config().weights.get("soglie_bias", {}) or {}
    strong = float(thresholds.get("strong", 40))
    normal = float(thresholds.get("normale", 15))
    if bias >= strong:
        return "STRONG_BULLISH"
    if bias >= normal:
        return "BULLISH"
    if bias <= -strong:
        return "STRONG_BEARISH"
    if bias <= -normal:
        return "BEARISH"
    return "NEUTRAL"


def build_context(
    session: Session,
    pair: str,
    evaluations: Dict[str, CurrencyEvaluation],
    horizon: Optional[str] = None,
) -> PairContext:
    config = get_config()
    horizon = horizon or config.default_horizon
    hours = config.horizon_hours(horizon)
    base, quote = config.split_pair(pair)

    base_eval = evaluations.get(base)
    quote_eval = evaluations.get(quote)
    if base_eval is None or quote_eval is None:
        raise ValueError(f"Valute non valutate per la coppia {pair}: {base}/{quote}")

    # BIAS di coppia = differenza degli score, normalizzata su [-100, +100].
    bias = max(-100.0, min(100.0, (base_eval.composite - quote_eval.composite) / 2.0))

    now = now_utc()
    # La finestra include le 2 ore appena trascorse: serve per il blocco "post-evento".
    events = events_between(session, now - timedelta(hours=2), now + timedelta(hours=hours), [base, quote])
    future_events = [e for e in events if e.timestamp_utc >= now]
    red_events = [e for e in future_events if e.impact == "RED"]
    next_red = red_events[0] if red_events else None

    # Rischio evento: 1.0 se un RED e' imminente, decrescente con la distanza.
    event_risk = 0.0
    for event in red_events:
        hours_ahead = max(0.0, (event.timestamp_utc - now).total_seconds() / 3600.0)
        event_risk = max(event_risk, math.exp(-hours_ahead / 18.0))
    if not red_events:
        for event in future_events:
            if event.impact == "ORANGE":
                hours_ahead = max(0.0, (event.timestamp_utc - now).total_seconds() / 3600.0)
                event_risk = max(event_risk, 0.4 * math.exp(-hours_ahead / 18.0))

    health = all_source_health(session)
    if health:
        ok = sum(1 for h in health if h.status == "OK")
        source_ratio = ok / len(health)
    else:
        source_ratio = 0.5

    return PairContext(
        pair=pair.upper(),
        base=base,
        quote=quote,
        base_eval=base_eval,
        quote_eval=quote_eval,
        bias=bias,
        bias_label=classify_bias(bias),
        horizon=horizon,
        horizon_hours=hours,
        events=events,
        future_events=future_events,
        red_events=red_events,
        next_red=next_red,
        event_risk=event_risk,
        source_ratio=source_ratio,
    )


def confidence_for(ctx: PairContext) -> Tuple[str, float, List[str], bool]:
    """Confidenza qualitativa + punteggio interno + motivazioni + flag riscrivibile."""
    config = get_config()
    conf_cfg = config.weights.get("confidenza", {}) or {}
    scen_cfg = config.weights.get("scenari", {}) or {}

    quality = (ctx.base_eval.data_quality + ctx.quote_eval.data_quality) / 2.0
    convergence = (ctx.base_eval.convergence() + ctx.quote_eval.convergence()) / 2.0

    score = (
        40.0 * quality
        + 35.0 * convergence
        + 15.0 * ctx.source_ratio
        + 10.0 * (1.0 - ctx.event_risk)
    )

    reasons = [
        f"Qualita' dati {quality:.0%}",
        f"convergenza sotto-score {convergence:.0%}",
        f"fonti operative {ctx.source_ratio:.0%}",
    ]

    if score >= float(conf_cfg.get("soglia_alta", 70)):
        level = ALTA
    elif score >= float(conf_cfg.get("soglia_media", 45)):
        level = MEDIA
    else:
        level = BASSA

    rewritable = False
    threshold_hours = float(scen_cfg.get("ore_evento_red_declassa", 24))
    if ctx.next_red is not None:
        hours_ahead = (ctx.next_red.timestamp_utc - now_utc()).total_seconds() / 3600.0
        if hours_ahead <= threshold_hours:
            level = _downgrade(level, 1)
            rewritable = True
            reasons.append(
                f"evento RED entro {max(0.0, hours_ahead):.1f}h ({ctx.next_red.title}): "
                "confidenza declassata, scenario riscrivibile"
            )
    return level, score, reasons, rewritable


# --------------------------------------------------------------------------- #
#  Driver, invalidazioni, suggerimenti
# --------------------------------------------------------------------------- #


def _drivers(ctx: PairContext, limit: int = 3) -> List[Dict[str, Any]]:
    """I sotto-score che contribuiscono di piu' al bias (max 3)."""
    weights = get_config().subscore_weights
    contributions: List[Tuple[float, Dict[str, Any]]] = []
    names = set(ctx.base_eval.subscores) | set(ctx.quote_eval.subscores)
    for name in names:
        base_sub = ctx.base_eval.subscores.get(name)
        quote_sub = ctx.quote_eval.subscores.get(name)
        base_value = base_sub.value if base_sub else 0.0
        quote_value = quote_sub.value if quote_sub else 0.0
        contribution = float(weights.get(name, 0.0)) * (base_value - quote_value) / 2.0
        if abs(contribution) < 0.5:
            continue
        motivation = " | ".join(
            filter(
                None,
                [
                    f"{ctx.base}: {base_sub.rationale}" if base_sub else "",
                    f"{ctx.quote}: {quote_sub.rationale}" if quote_sub else "",
                ],
            )
        )
        contributions.append(
            (
                abs(contribution),
                {
                    "sottoscore": SUBSCORE_LABELS.get(name, name),
                    "contributo_bias": round(contribution, 1),
                    "favorisce": ctx.base if contribution > 0 else ctx.quote,
                    "motivazione": motivation[:400],
                },
            )
        )
    contributions.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in contributions[:limit]]


def _pivot_event(ctx: PairContext) -> Optional[Event]:
    """L'evento imminente piu' rilevante: definisce gli scenari alternativi."""
    if ctx.red_events:
        return ctx.red_events[0]
    orange = [e for e in ctx.future_events if e.impact == "ORANGE"]
    return orange[0] if orange else None


def _invalidation(ctx: PairContext, direction: str, drivers: List[Dict[str, Any]]) -> List[str]:
    """Condizioni di invalidazione esplicite e verificabili."""
    conditions: List[str] = []
    favoured = ctx.base if direction == RIALZO else ctx.quote
    opposite = ctx.quote if direction == RIALZO else ctx.base

    for driver in drivers[:2]:
        conditions.append(
            f"Il sotto-score '{driver['sottoscore']}' smette di favorire {driver['favorisce']} "
            f"(inversione del contributo, oggi {driver['contributo_bias']:+.1f})."
        )

    pivot = _pivot_event(ctx)
    if pivot is not None:
        book = playbook_for(pivot.category, pivot.currency)
        expected = "sopra" if pivot.currency == favoured else "sotto"
        conditions.append(
            f"{pivot.title} ({pivot.currency}, {pivot.timestamp_utc:%d/%m %H:%M} UTC) esce "
            f"{expected} le attese in modo marcato (|z| ≥ 2): "
            f"{book.get('nome', pivot.category)} riscrive il quadro."
        )

    if direction != LATERALE:
        conditions.append(
            f"Comunicato o discorso della banca centrale {opposite} con tono nettamente "
            f"opposto (tono ≥ 40 in favore di {opposite})."
        )
        conditions.append(
            f"Il bias di coppia rientra sotto la soglia di neutralita' "
            f"(|bias| < {get_config().weights.get('soglie_bias', {}).get('normale', 15)})."
        )
    else:
        conditions.append(
            "Il bias esce dalla zona neutra in una delle due direzioni (|bias| ≥ soglia)."
        )
    return conditions


def _suggestion(
    ctx: PairContext, direction: str, confidence: str, rewritable: bool
) -> Dict[str, str]:
    """Suggerimento operativo NON vincolante, coerente con lo stile configurato."""
    config = get_config()
    style = str(config.operator.get("stile", "no_trade_news"))
    risk = config.operator.get("rischio_per_trade_pct", 0.5)

    in_window, window_event, _ = no_trade_window(ctx.events)

    if in_window and window_event is not None:
        return {
            "azione": "STARE_FUORI",
            "motivazione": (
                f"Finestra di no-trade attiva per {window_event.title} ({window_event.currency}): "
                "lo spread si allarga e lo slippage diventa non modellabile."
            ),
        }

    # Il caso LATERALE va PRIMA del caso confidenza BASSA: da quando un
    # laterale senza segnale e' sempre BASSA, l'ordine inverso avrebbe
    # suggerito "solo post evento" dove la risposta onesta e' "stare fuori".
    if direction == LATERALE:
        return {
            "azione": "STARE_FUORI",
            "motivazione": "Bias in zona neutra: nessun vantaggio fondamentale identificabile.",
        }

    if confidence == BASSA:
        return {
            "azione": "SOLO_POST_EVENTO",
            "motivazione": (
                "Confidenza BASSA: dati incompleti o sotto-score discordi. "
                f"Se si opera, rischio ridotto rispetto allo standard ({risk}%)."
            ),
        }

    action = "FAVORIRE_LONG" if direction == RIALZO else "FAVORIRE_SHORT"
    reasons = {
        "no_trade_news": (
            "Operare fuori dalle finestre evento, in direzione del bias fondamentale."
        ),
        "news_fade": (
            "Stile news-fade: attendere l'esaurimento dello spike e cercare il rientro "
            "in direzione del bias fondamentale."
        ),
        "trend_continuation": (
            "Stile trend-continuation: cercare continuazione post-evento solo se il dato "
            "conferma la direzione del bias."
        ),
    }
    motivation = reasons.get(style, reasons["no_trade_news"])
    if rewritable:
        motivation += " Attenzione: evento RED entro le prossime ore, size ridotta."
    return {"azione": action, "motivazione": motivation}


# --------------------------------------------------------------------------- #
#  Probabilita'
# --------------------------------------------------------------------------- #


def _probabilities(
    ctx: PairContext, confidence_score: float, align_a: int, align_b: int
) -> Tuple[float, float, float]:
    """Probabilita' dei tre scenari (somma 1.0), con cap strutturale sul base."""
    cfg = get_config().weights.get("scenari", {}) or {}
    p_min = float(cfg.get("prob_base_min", 0.38))
    p_max = float(cfg.get("prob_base_max", 0.65))
    alt_min = float(cfg.get("prob_alt_min", 0.10))

    strength = 0.6 * min(1.0, abs(ctx.bias) / 60.0) + 0.4 * max(
        0.0, min(1.0, confidence_score / 100.0)
    )
    # Il rischio evento comprime la probabilita' del base verso il minimo.
    strength *= 1.0 - 0.35 * ctx.event_risk
    p_base = p_min + (p_max - p_min) * strength
    p_base = max(p_min, min(p_max, p_base))

    remainder = 1.0 - p_base
    share_a = 0.5 + 0.10 * align_a - 0.10 * align_b
    share_a = max(0.25, min(0.75, share_a))
    p_a = remainder * share_a
    p_b = remainder - p_a

    # Nessuno scenario alternativo puo' essere schiacciato sotto il minimo.
    if p_a < alt_min:
        p_b -= alt_min - p_a
        p_a = alt_min
    if p_b < alt_min:
        p_a -= alt_min - p_b
        p_b = alt_min

    total = p_base + p_a + p_b
    p_base, p_a, p_b = p_base / total, p_a / total, p_b / total

    # Compressione verso 1/3, a somma invariata: la rigiocata 2007-2026 ha
    # misurato probabilita' dichiarate ~45% contro frequenze reali ~21%
    # (scarto -24 punti). lambda=1 lascia le probabilita' grezze, lambda=0
    # le appiattisce tutte al 33%: il default sta a meta', e va rimisurato.
    lam = max(0.0, min(1.0, float(cfg.get("shrinkage_lambda", 1.0))))
    terzo = 1.0 / 3.0
    p_base = terzo + lam * (p_base - terzo)
    p_a = terzo + lam * (p_a - terzo)
    p_b = terzo + lam * (p_b - terzo)
    return p_base, p_a, p_b


# --------------------------------------------------------------------------- #
#  Generazione
# --------------------------------------------------------------------------- #


def _pending_events_payload(ctx: PairContext, limit: int = 5) -> List[Dict[str, Any]]:
    payload = []
    now = now_utc()
    for event in ctx.future_events[:limit]:
        payload.append(
            {
                "titolo": event.title,
                "valuta": event.currency,
                "impatto": event.impact,
                "categoria": event.category,
                "timestamp_utc": event.timestamp_utc.isoformat() + "Z",
                "ore_mancanti": round((event.timestamp_utc - now).total_seconds() / 3600.0, 1),
                "volatilita_attesa": playbook_for(event.category, event.currency).get(
                    "volatilita_attesa", "MEDIA"
                ),
            }
        )
    return payload


def generate_pair_scenarios(
    session: Session,
    pair: str,
    evaluations: Dict[str, CurrencyEvaluation],
    horizon: Optional[str] = None,
) -> Dict[str, Any]:
    """Genera il pacchetto completo di scenari per una coppia."""
    config = get_config()
    ctx = build_context(session, pair, evaluations, horizon)
    confidence, confidence_score, confidence_reasons, rewritable = confidence_for(ctx)
    drivers = _drivers(ctx)

    # La soglia per DICHIARARE una direzione e' separata (e piu' bassa) da
    # quella dell'etichetta BULLISH/BEARISH: con la soglia unica a 15 il
    # modello diceva LATERALE nel 97,6% dei casi contro ~20% di laterali reali
    # (misurato su 20.901 scenari rigiocati). Vedi weights.yaml -> direzione.
    soglie = config.weights.get("soglie_bias", {}) or {}
    neutral_threshold = float(soglie.get("direzione", soglie.get("normale", 15)))
    base_direction = _direction_from_bias(ctx.bias, neutral_threshold)

    if base_direction == LATERALE:
        # Misurato in rigiocata: le previsioni LATERALE sono la classe con
        # l'hit-rate peggiore (~20%) e concentravano la confidenza ALTA,
        # invertendo la scala (ALTA 18% < BASSA 24%). Un laterale dichiarato
        # per assenza di segnale direzionale non merita mai piu' di BASSA.
        confidence = BASSA
        confidence_reasons.append(
            "direzione LATERALE per assenza di segnale: confidenza limitata a BASSA "
            "(in rigiocata la classe LATERALE e' quella con l'hit-rate piu' basso)"
        )

    pivot = _pivot_event(ctx)
    pivot_currency = pivot.currency if pivot is not None else (
        ctx.base if abs(ctx.base_eval.composite) >= abs(ctx.quote_eval.composite) else ctx.quote
    )
    if pivot_currency not in (ctx.base, ctx.quote):
        pivot_currency = ctx.base

    # ALT A — sorpresa hawkish / risk-on sulla valuta pivot: il pivot si rafforza.
    dir_a = RIALZO if pivot_currency == ctx.base else RIBASSO
    # ALT B — sorpresa dovish sul pivot + avversione al rischio: il pivot si indebolisce,
    # ma il bene rifugio della coppia viene comprato. Se i due canali confliggono
    # lo scenario resta valido con magnitudo ridotta e il conflitto viene dichiarato.
    dir_b_dovish = RIBASSO if pivot_currency == ctx.base else RIALZO
    haven_delta = SAFE_HAVEN_RANK.get(ctx.base, 0.0) - SAFE_HAVEN_RANK.get(ctx.quote, 0.0)
    dir_b_haven = RIALZO if haven_delta > 0 else (RIBASSO if haven_delta < 0 else dir_b_dovish)
    conflict_b = dir_b_haven != dir_b_dovish
    dir_b = dir_b_dovish

    align_a = 1 if (dir_a == base_direction and base_direction != LATERALE) else -1
    align_b = 1 if (dir_b == base_direction and base_direction != LATERALE) else -1
    p_base, p_a, p_b = _probabilities(ctx, confidence_score, align_a, align_b)

    chains = merge_activations(
        [
            *[
                ChainActivation(
                    chain_id=c.chain_id,
                    direction=c.direction,
                    strength=c.strength,
                    trigger=c.trigger,
                    currency=ctx.base,
                    sources=c.sources,
                )
                for c in ctx.base_eval.chains
            ],
            *[
                # Per la valuta quotata l'effetto sulla COPPIA e' invertito.
                ChainActivation(
                    chain_id=c.chain_id,
                    direction=c.direction,
                    strength=c.strength,
                    trigger=c.trigger,
                    currency=ctx.quote,
                    sources=c.sources,
                )
                for c in ctx.quote_eval.chains
            ],
        ]
    )

    now = now_utc()
    validity_hours = float(
        (config.weights.get("scenari", {}) or {}).get("validita_ore", {}).get(ctx.horizon, 12)
    )
    expires_at = now + timedelta(hours=validity_hours)
    horizon_end = now + timedelta(hours=ctx.horizon_hours)

    pivot_label = (
        f"{pivot.title} ({pivot.currency})" if pivot is not None else f"dati {pivot_currency}"
    )

    scenarios = [
        {
            "tipo": "BASE",
            "nome": "Scenario base",
            "probabilita": round(p_base * 100, 1),
            "direzione": base_direction,
            "magnitudo": _magnitude(abs(ctx.bias), ctx.event_risk),
            "confidenza": confidence,
            "descrizione": (
                f"Il differenziale fondamentale fra {ctx.base} e {ctx.quote} "
                f"({ctx.bias:+.1f}, {ctx.bias_label}) resta il driver dominante "
                f"sull'orizzonte {config.horizons.get(ctx.horizon, {}).get('etichetta', ctx.horizon)}."
            ),
            "driver": drivers,
            "catene_causali": chains[:6],
            "invalidazione": _invalidation(ctx, base_direction, drivers),
            "suggerimento": _suggestion(ctx, base_direction, confidence, rewritable),
        },
        {
            "tipo": "ALT_A",
            "nome": "Alternativo A — sorpresa hawkish / risk-on",
            "probabilita": round(p_a * 100, 1),
            "direzione": dir_a,
            "magnitudo": _magnitude(abs(ctx.bias) * 0.7 + 25, ctx.event_risk),
            "confidenza": _downgrade(confidence) if confidence == ALTA else confidence,
            "descrizione": (
                f"{pivot_label} sorprende in senso restrittivo (|z| ≥ 2): il canale "
                f"tassi→capitali→cambio si attiva a favore di {pivot_currency}, che si apprezza."
            ),
            "driver": [
                {
                    "sottoscore": "Sorprese recenti",
                    "contributo_bias": None,
                    "favorisce": pivot_currency,
                    "motivazione": f"Sorpresa hawkish attesa su {pivot_label}.",
                }
            ],
            "catene_causali": [
                {
                    "id": "TASSI_CAPITALI_CAMBIO",
                    "nome": "Tassi → capitali → cambio",
                    "valuta": pivot_currency,
                    "direzione": "positiva",
                    "effetto_valuta": "APPREZZAMENTO",
                    "intensita": 0.8,
                    "trigger": f"Sorpresa restrittiva su {pivot_label}",
                    "passi": [
                        "dato sopra attese → attese di tassi piu' alti",
                        "afflussi di capitale ↑",
                        "domanda della valuta ↑",
                        "APPREZZAMENTO",
                    ],
                    "fonti": [pivot.source] if pivot is not None else [],
                }
            ],
            "invalidazione": [
                f"{pivot_label} esce in linea o sotto le attese: lo scenario decade immediatamente.",
                f"La banca centrale di {pivot_currency} accompagna il dato con guidance accomodante.",
            ],
            "suggerimento": _suggestion(ctx, dir_a, _downgrade(confidence), rewritable),
        },
        {
            "tipo": "ALT_B",
            "nome": "Alternativo B — sorpresa dovish / risk-off",
            "probabilita": round(p_b * 100, 1),
            "direzione": dir_b,
            "magnitudo": _magnitude(
                (abs(ctx.bias) * 0.7 + 25) * (0.6 if conflict_b else 1.0), ctx.event_risk
            ),
            "confidenza": _downgrade(confidence) if confidence == ALTA else confidence,
            "descrizione": (
                f"{pivot_label} sorprende in senso accomodante: attese di easing su "
                f"{pivot_currency}, che si indebolisce."
                + (
                    f" Canale in conflitto: in avversione al rischio il flusso verso i beni "
                    f"rifugio spingerebbe la coppia in direzione {dir_b_haven}. "
                    "Magnitudo ridotta e confidenza gia' declassata."
                    if conflict_b
                    else " In avversione al rischio i due canali si sommano."
                )
            ),
            "driver": [
                {
                    "sottoscore": "Stance banca centrale",
                    "contributo_bias": None,
                    "favorisce": ctx.quote if dir_b == RIBASSO else ctx.base,
                    "motivazione": f"Sorpresa dovish attesa su {pivot_label}.",
                }
            ],
            "catene_causali": [
                {
                    "id": "MONETARIA_ESPANSIVA",
                    "nome": "Politica monetaria espansiva (cambi flessibili)",
                    "valuta": pivot_currency,
                    "direzione": "positiva",
                    "effetto_valuta": "DEPREZZAMENTO",
                    "intensita": 0.75,
                    "trigger": f"Sorpresa accomodante su {pivot_label}",
                    "passi": [
                        "dato sotto attese / guidance accomodante",
                        "attese di i ↓",
                        "minore afflusso di capitali",
                        "DEPREZZAMENTO della valuta",
                    ],
                    "fonti": [pivot.source] if pivot is not None else [],
                }
            ],
            "invalidazione": [
                f"{pivot_label} esce in linea o sopra le attese.",
                "Il mercato interpreta il dato debole come pro-rischio (easing = risk-on): "
                "in quel caso l'effetto sui beni rifugio si inverte.",
            ],
            "suggerimento": _suggestion(ctx, dir_b, _downgrade(confidence), rewritable),
        },
    ]

    return {
        "coppia": ctx.pair,
        "orizzonte": ctx.horizon,
        "orizzonte_etichetta": config.horizons.get(ctx.horizon, {}).get(
            "etichetta", ctx.horizon
        ),
        "generato_il": now.isoformat() + "Z",
        "scade_il": expires_at.isoformat() + "Z",
        "fine_orizzonte": horizon_end.isoformat() + "Z",
        "bias": round(ctx.bias, 1),
        "bias_label": ctx.bias_label,
        "score_base": round(ctx.base_eval.composite, 1),
        "score_quote": round(ctx.quote_eval.composite, 1),
        "confidenza": confidence,
        "confidenza_score": round(confidence_score, 1),
        "confidenza_motivazioni": confidence_reasons,
        "riscrivibile": rewritable,
        "rischio_evento": round(ctx.event_risk, 2),
        # Parametri di calibrazione applicati a QUESTO scenario, dichiarati
        # perche' rigiocate con parametri diversi non sono confrontabili.
        "calibrazione": {
            "soglia_direzione": neutral_threshold,
            "shrinkage_lambda": max(
                0.0,
                min(
                    1.0,
                    float(
                        (config.weights.get("scenari", {}) or {}).get("shrinkage_lambda", 1.0)
                    ),
                ),
            ),
            "nota": (
                "Soglia di direzione e compressione delle probabilita' ricalibrate "
                "sulla rigiocata 2007-2026 (20.901 scenari valutati)."
            ),
        },
        "eventi_rilevanti": _pending_events_payload(ctx),
        "scenari": scenarios,
        "disclaimer": (
            "Scenari probabilistici generati da modello. Non costituiscono consulenza "
            "finanziaria. La probabilita' e' una stima soggetta a errore."
        ),
    }


def persist_scenario(session: Session, payload: Dict[str, Any]) -> Scenario:
    """Storicizza lo scenario generato (SEZIONE 7 — calibrazione)."""
    base = payload["scenari"][0]
    row = Scenario(
        pair=payload["coppia"],
        horizon=payload["orizzonte"],
        generated_at=datetime.fromisoformat(payload["generato_il"].rstrip("Z")),
        expires_at=datetime.fromisoformat(payload["scade_il"].rstrip("Z")),
        horizon_end=datetime.fromisoformat(payload["fine_orizzonte"].rstrip("Z")),
        bias=float(payload["bias"]),
        bias_label=payload["bias_label"],
        confidence=payload["confidenza"],
        confidence_score=float(payload["confidenza_score"]),
        base_direction=base["direzione"],
        base_probability=float(base["probabilita"]) / 100.0,
        alt_a_probability=float(payload["scenari"][1]["probabilita"]) / 100.0,
        alt_b_probability=float(payload["scenari"][2]["probabilita"]) / 100.0,
        payload=payload,
        rewritable=bool(payload["riscrivibile"]),
    )
    session.add(row)
    return row


def generate_all(
    session: Session,
    evaluations: Dict[str, CurrencyEvaluation],
    horizon: Optional[str] = None,
    persist: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """Genera (e opzionalmente storicizza) gli scenari di tutte le coppie configurate."""
    config = get_config()
    out: Dict[str, Dict[str, Any]] = {}
    for pair in config.pairs:
        try:
            payload = generate_pair_scenarios(session, pair, evaluations, horizon)
        except Exception:  # pragma: no cover - difensivo
            logger.exception("Errore nella generazione degli scenari per %s", pair)
            continue
        out[pair] = payload
        if persist:
            persist_scenario(session, payload)
    return out
