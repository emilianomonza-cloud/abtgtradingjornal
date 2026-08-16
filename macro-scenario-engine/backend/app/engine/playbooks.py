"""SEZIONE 8 — Accesso ai playbook eventi e calcolo delle finestre di no-trade."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..config import get_config
from ..storage.models import Event
from ..storage.repositories import now_utc

_DEFAULT_PLAYBOOK: Dict[str, Any] = {
    "nome": "Evento non modellato",
    "valuta_primaria": None,
    "valute_secondarie": [],
    "catena_attesa": None,
    "catene_secondarie": [],
    "descrizione": (
        "Nessun playbook configurato per questa categoria: l'evento viene mostrato "
        "in calendario ma non alimenta gli score."
    ),
    "reazione": {},
    "eccezioni": [],
    "volatilita_attesa": "BASSA",
    "no_trade_minuti": {"prima": 0, "dopo": 0},
    "second_order": [],
}


def playbook_for(category: str, currency: Optional[str] = None) -> Dict[str, Any]:
    """Playbook della categoria, con eventuale specializzazione per valuta.

    La specializzazione (es. RATE_DECISION:USD = FOMC) eredita dal playbook di
    categoria e ne sovrascrive solo i campi presenti.
    """
    config = get_config()
    books = config.playbooks.get("playbooks", {}) or {}
    specials = config.playbooks.get("specializzazioni", {}) or {}

    base = dict(_DEFAULT_PLAYBOOK)
    base.update(dict(books.get(category, {}) or {}))

    if currency:
        special = specials.get(f"{category}:{currency.upper()}")
        if special:
            base.update(dict(special))
    base.setdefault("categoria", category)
    return base


def expected_reaction(category: str, currency: str, surprise_label: str, regime: str) -> str:
    """Reazione attesa da playbook per una data sorpresa e un dato regime."""
    book = playbook_for(category, currency)
    reactions = book.get("reazione", {}) or {}
    key = surprise_label
    if key in ("BEAT", "BIG_BEAT"):
        key = "BIG_BEAT"
    elif key in ("MISS", "BIG_MISS"):
        key = "BIG_MISS"
    else:
        return "Dato in linea con le attese: nessuna riscrittura del quadro."
    branch = reactions.get(key, {}) or {}
    return str(branch.get(regime.upper(), branch.get("HAWKISH_DOMINANT", "Reazione non codificata.")))


def exceptions_for(category: str, currency: str) -> List[str]:
    return list(playbook_for(category, currency).get("eccezioni", []) or [])


def second_order_for(category: str, currency: str) -> List[str]:
    return list(playbook_for(category, currency).get("second_order", []) or [])


def _window_minutes(event: Event) -> Tuple[int, int]:
    """Minuti di blocco prima/dopo l'evento: playbook se presente, altrimenti weights.yaml."""
    book = playbook_for(event.category, event.currency)
    window = book.get("no_trade_minuti") or {}
    fallback = (get_config().weights.get("no_trade", {}) or {}).get(event.impact, {}) or {}
    before = int(window.get("prima", fallback.get("prima", 0)) or 0)
    after = int(window.get("dopo", fallback.get("dopo", 0)) or 0)
    # Un evento a basso impatto non blocca mai piu' di quanto previsto per il suo livello.
    if event.impact == "YELLOW":
        before = min(before, int(fallback.get("prima", 0) or 0))
        after = min(after, int(fallback.get("dopo", 0) or 0))
    return before, after


def no_trade_window(
    events: Sequence[Event], reference: Optional[Any] = None
) -> Tuple[bool, Optional[Event], float]:
    """Verifica se il momento corrente cade in una finestra di blocco.

    Ritorna (attiva, evento, minuti_alla_fine_della_finestra).
    Considera sia gli eventi futuri (finestra 'prima') sia quelli appena usciti
    (finestra 'dopo'), quindi la lista in ingresso deve includere il passato recente.
    """
    now = reference or now_utc()
    active: Optional[Event] = None
    remaining = 0.0
    for event in events:
        before, after = _window_minutes(event)
        if before == 0 and after == 0:
            continue
        delta_minutes = (event.timestamp_utc - now).total_seconds() / 60.0
        if -after <= delta_minutes <= before:
            end_in = delta_minutes + after
            if active is None or end_in > remaining:
                active = event
                remaining = end_in
    return (active is not None), active, max(0.0, remaining)


def next_no_trade_start(events: Sequence[Event]) -> Optional[Tuple[Event, float]]:
    """Prossima finestra di blocco che si aprira': (evento, minuti mancanti)."""
    now = now_utc()
    best: Optional[Tuple[Event, float]] = None
    for event in events:
        before, _ = _window_minutes(event)
        if before <= 0:
            continue
        minutes_to_start = (event.timestamp_utc - now).total_seconds() / 60.0 - before
        if minutes_to_start <= 0:
            continue
        if best is None or minutes_to_start < best[1]:
            best = (event, minutes_to_start)
    return best


def window_for_event(event: Event) -> Dict[str, int]:
    before, after = _window_minutes(event)
    return {"prima": before, "dopo": after}


def all_playbooks() -> Dict[str, Any]:
    config = get_config()
    return {
        "playbooks": config.playbooks.get("playbooks", {}) or {},
        "specializzazioni": config.playbooks.get("specializzazioni", {}) or {},
    }


def categories_with_playbook() -> Iterable[str]:
    return (get_config().playbooks.get("playbooks", {}) or {}).keys()
