"""Pipeline applicativa: score → scenari → snapshot MT5.

Espone anche una cache in memoria dello snapshot, cosi' che
GET /api/v1/mt5/snapshot risponda in pochi millisecondi anche mentre lo
scheduler sta ricalcolando (criterio di accettazione: < 200 ms).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .config import get_config
from .engine import playbooks, scenarios as scenario_engine, scoring
from .storage.database import session_scope
from .storage.models import Event
from .storage.repositories import all_source_health, events_between, now_utc

logger = logging.getLogger(__name__)


class SnapshotCache:
    """Cache thread-safe dell'ultimo stato calcolato."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scores: Dict[str, Any] = {}
        self._scenarios: Dict[str, Dict[str, Any]] = {}
        self._mt5: Dict[str, Any] = {}
        self._updated_at: Optional[datetime] = None

    def update(
        self,
        scores: Dict[str, Any],
        scenarios: Dict[str, Dict[str, Any]],
        mt5: Dict[str, Any],
    ) -> None:
        with self._lock:
            self._scores = scores
            self._scenarios = scenarios
            self._mt5 = mt5
            self._updated_at = now_utc()

    @property
    def scores(self) -> Dict[str, Any]:
        with self._lock:
            return self._scores

    @property
    def scenarios(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return self._scenarios

    @property
    def mt5(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._mt5)

    @property
    def updated_at(self) -> Optional[datetime]:
        with self._lock:
            return self._updated_at

    @property
    def age_seconds(self) -> Optional[float]:
        updated = self.updated_at
        if updated is None:
            return None
        return (now_utc() - updated).total_seconds()


cache = SnapshotCache()


# --------------------------------------------------------------------------- #
#  Snapshot MT5 (SEZIONE 6)
# --------------------------------------------------------------------------- #


def _clean(text: str, limit: int = 60) -> str:
    """Ripulisce una stringa destinata al parser JSON minimale di MQL5.

    Il parser incluso nel bridge non gestisce escape: virgolette, backslash e
    caratteri di controllo vengono rimossi qui, alla fonte.
    """
    cleaned = "".join(
        ch for ch in str(text) if ch.isprintable() and ch not in ('"', "\\")
    )
    return cleaned.strip()[:limit]


def _pair_events(session: Session, base: str, quote: str, hours: int = 48) -> List[Event]:
    now = now_utc()
    return events_between(
        session, now - timedelta(hours=2), now + timedelta(hours=hours), [base, quote]
    )


def build_mt5_snapshot(
    session: Session,
    evaluations: Dict[str, scoring.CurrencyEvaluation],
    generated: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Payload compatto e stabile per il bridge MQL5.

    Le chiavi sono corte e lo schema e' FISSO: il parser MQL5 incluso nel
    progetto non usa librerie esterne e si appoggia a questo schema.
    """
    config = get_config()
    now = now_utc()

    currencies = [
        {
            "c": ev.currency,
            "s": round(ev.composite, 1),
            "q": round(ev.data_quality, 2),
        }
        for ev in evaluations.values()
    ]

    pairs_payload: List[Dict[str, Any]] = []
    events_payload: List[Dict[str, Any]] = []
    seen_events: set[int] = set()

    for pair, payload in generated.items():
        base, quote = config.split_pair(pair)
        events = _pair_events(session, base, quote)
        future_red = [
            e for e in events if e.impact == "RED" and e.timestamp_utc >= now
        ]
        minutes_to_red = -1
        red_title = ""
        if future_red:
            nearest = min(future_red, key=lambda e: e.timestamp_utc)
            minutes_to_red = int(
                (nearest.timestamp_utc - now).total_seconds() // 60
            )
            red_title = _clean(f"{nearest.currency} {nearest.title}")

        in_window, window_event, remaining = playbooks.no_trade_window(events)
        base_scenario = payload["scenari"][0]

        pairs_payload.append(
            {
                "p": pair,
                "bias": payload["bias"],
                "lab": payload["bias_label"],
                "dir": base_scenario["direzione"],
                "p0": base_scenario["probabilita"],
                "p1": payload["scenari"][1]["probabilita"],
                "p2": payload["scenari"][2]["probabilita"],
                "conf": payload["confidenza"],
                "exp": payload["scade_il"],
                "rw": 1 if payload["riscrivibile"] else 0,
                "redm": minutes_to_red,
                "redt": red_title,
                "nt": 1 if in_window else 0,
                "ntm": int(remaining),
                "nte": (_clean(window_event.title) if window_event is not None else ""),
                "act": base_scenario["suggerimento"]["azione"],
            }
        )

        for event in events:
            if event.timestamp_utc < now or event.id in seen_events:
                continue
            if event.impact not in ("RED", "ORANGE"):
                continue
            seen_events.add(event.id)
            events_payload.append(
                {
                    "c": event.currency,
                    "t": _clean(event.title),
                    "im": event.impact,
                    "min": int((event.timestamp_utc - now).total_seconds() // 60),
                    "ts": event.timestamp_utc.isoformat() + "Z",
                }
            )

    events_payload.sort(key=lambda e: e["min"])

    health = all_source_health(session)
    degraded = [h.name for h in health if h.status != "OK"]

    return {
        "v": 1,
        "ts": now.isoformat() + "Z",
        "tz": config.operator.get("fuso_orario", "Europe/Rome"),
        "hz": config.default_horizon,
        "ccy": currencies,
        "pairs": pairs_payload,
        "events": events_payload[:20],
        "fonti_degradate": degraded,
        "disclaimer": (
            "Scenari probabilistici generati da modello. Non costituiscono "
            "consulenza finanziaria."
        ),
    }


# --------------------------------------------------------------------------- #
#  Ciclo completo
# --------------------------------------------------------------------------- #


def refresh(
    session: Session, horizon: Optional[str] = None, persist: bool = True
) -> Dict[str, Any]:
    """Ricalcola score e scenari, aggiorna la cache e ritorna un riepilogo."""
    evaluations = scoring.evaluate_all(session)
    if persist:
        scoring.persist_scores(session, evaluations)

    generated = scenario_engine.generate_all(session, evaluations, horizon, persist=persist)
    snapshot = build_mt5_snapshot(session, evaluations, generated)

    scores_payload = {
        currency: {
            **evaluation.to_dict(),
            "variazione_24h": _round_or_none(
                scoring.score_change_24h(session, currency, evaluation.composite)
            ),
        }
        for currency, evaluation in evaluations.items()
    }

    cache.update(scores_payload, generated, snapshot)
    return {
        "valute": len(evaluations),
        "coppie": len(generated),
        "aggiornato_il": snapshot["ts"],
    }


def _round_or_none(value: Optional[float]) -> Optional[float]:
    return round(value, 1) if value is not None else None


def refresh_in_new_session(horizon: Optional[str] = None) -> Dict[str, Any]:
    """Variante usata dallo scheduler: apre e chiude la sua sessione."""
    with session_scope() as session:
        return refresh(session, horizon)


def ensure_fresh(session: Session, max_age_seconds: float = 900.0) -> None:
    """Ricalcola se la cache e' assente o piu' vecchia della soglia."""
    age = cache.age_seconds
    if age is None or age > max_age_seconds:
        refresh(session)
