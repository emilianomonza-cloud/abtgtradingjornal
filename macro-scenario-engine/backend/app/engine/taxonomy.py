"""Normalizzazione dei dati grezzi di calendario: valori, impatto, categoria."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from ..config import get_config

# ----------------------------------------------------------------- valori ----

_NUM_RE = re.compile(r"[-+]?\d*[.,]?\d+")
_MULTIPLIERS = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def parse_value(raw: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    """Converte un valore di calendario in numero + unita'.

    '236K' → (236.0, 'K')      | '3.2%'  → (3.2, '%')
    '-1.5B' → (-1.5, 'B')      | '<0.1%' → (0.1, '%')
    ''/None/'--' → (None, None)

    I moltiplicatori NON vengono espansi: forecast e actual dello stesso indicatore
    condividono l'unita', quindi confrontarli in scala nativa e' corretto e
    mantiene le sigma storiche sulla stessa scala.
    """
    if raw is None:
        return None, None
    text = str(raw).strip()
    if text in ("", "-", "--", "n/a", "N/A"):
        return None, None

    unit: Optional[str] = None
    upper = text.upper()
    for suffix, _ in _MULTIPLIERS.items():
        if upper.endswith(suffix):
            unit = suffix
            break
    if unit is None and "%" in text:
        unit = "%"

    match = _NUM_RE.search(text.replace(",", "."))
    if match is None:
        return None, unit
    try:
        value = float(match.group(0))
    except ValueError:
        return None, unit

    # Segno negativo espresso fra parentesi: (1.5) → -1.5
    if text.startswith("(") and text.endswith(")"):
        value = -abs(value)
    return value, unit


_IMPACT_MAP = {
    "high": "RED",
    "red": "RED",
    "3": "RED",
    "medium": "ORANGE",
    "orange": "ORANGE",
    "2": "ORANGE",
    "low": "YELLOW",
    "yellow": "YELLOW",
    "1": "YELLOW",
    "holiday": "YELLOW",
    "non-economic": "YELLOW",
    "grey": "YELLOW",
    "gray": "YELLOW",
}


def normalize_impact(raw: Optional[str]) -> str:
    if raw is None:
        return "YELLOW"
    return _IMPACT_MAP.get(str(raw).strip().lower(), "YELLOW")


# -------------------------------------------------------------- categorie ----


@lru_cache(maxsize=1)
def _compiled_patterns() -> List[Tuple[str, re.Pattern]]:
    """Compila i pattern di indicators.yaml, ordinati per specificita'.

    I pattern piu' lunghi vengono provati per primi cosi' che 'core cpi' vinca
    su 'cpi' e 'ism services' su 'services pmi'.
    """
    config = get_config()
    categories: Dict[str, dict] = config.indicators.get("categorie", {}) or {}
    compiled: List[Tuple[str, re.Pattern, int]] = []
    for category, spec in categories.items():
        for pattern in spec.get("pattern", []) or []:
            compiled.append((category, re.compile(pattern, re.IGNORECASE), len(pattern)))
    compiled.sort(key=lambda item: item[2], reverse=True)
    return [(cat, rx) for cat, rx, _ in compiled]


def clear_taxonomy_cache() -> None:
    """Da chiamare dopo un reload della configurazione."""
    _compiled_patterns.cache_clear()


def classify_category(title: str) -> str:
    """Assegna la categoria normalizzata a partire dal titolo dell'evento."""
    config = get_config()
    text = (title or "").strip().lower()
    if not text:
        return str(config.indicators.get("categoria_default", "OTHER"))
    for category, pattern in _compiled_patterns():
        if pattern.search(text):
            return category
    return str(config.indicators.get("categoria_default", "OTHER"))


def category_spec(category: str) -> Dict[str, object]:
    config = get_config()
    categories = config.indicators.get("categorie", {}) or {}
    return dict(categories.get(category, {}) or {})


def category_label(category: str) -> str:
    spec = category_spec(category)
    return str(spec.get("etichetta", category))


def category_direction(category: str) -> int:
    """+1 se 'valore piu' alto = valuta piu' forte', -1 se invertito."""
    spec = category_spec(category)
    try:
        return 1 if int(spec.get("direzione", 1)) >= 0 else -1
    except (TypeError, ValueError):
        return 1


def category_family(category: str) -> str:
    return str(category_spec(category).get("famiglia", "ciclici"))


def category_subscore(category: str) -> Optional[str]:
    value = category_spec(category).get("sottoscore")
    return str(value) if value else None


def category_sigma(category: str) -> float:
    try:
        return float(category_spec(category).get("sigma_default", 1.0)) or 1.0
    except (TypeError, ValueError):
        return 1.0


def category_neutral_level(category: str) -> Optional[float]:
    value = category_spec(category).get("livello_neutro")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
