"""SEZIONE 3.4 — Classificatore semantico rule-based (fallback sempre attivo).

Non richiede chiavi API: il sistema deve funzionare completamente anche senza LLM.
Riconosce il tono hawkish/dovish tramite dizionario pesato, con gestione di
negazioni e intensificatori, ed estrae temi e sintesi in italiano.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Tuple

from ..config import get_config

HAWKISH = "HAWKISH"
DOVISH = "DOVISH"
NEUTRAL = "NEUTRAL"

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_TOKEN = re.compile(r"[a-zàèéìòùâêîôûäöüç']+", re.IGNORECASE)


@dataclass
class ToneResult:
    """Esito della classificazione semantica di un testo."""

    tone_score: float  # [-100, +100]
    tone_label: str
    themes: List[str] = field(default_factory=list)
    summary_it: str = ""
    matched_terms: List[Dict[str, object]] = field(default_factory=list)
    classifier: str = "rule_based"

    def to_dict(self) -> Dict[str, object]:
        return {
            "tono": round(self.tone_score, 1),
            "etichetta": self.tone_label,
            "temi": self.themes,
            "sintesi_it": self.summary_it,
            "termini": self.matched_terms,
            "classificatore": self.classifier,
        }


@lru_cache(maxsize=1)
def _lexicon_terms() -> List[Tuple[str, float, re.Pattern]]:
    """Termini del dizionario compilati, dal piu' lungo al piu' corto."""
    lexicon = get_config().lexicon
    entries: List[Tuple[str, float, re.Pattern]] = []
    for bucket in ("hawkish", "dovish"):
        for term, weight in (lexicon.get(bucket, {}) or {}).items():
            pattern = re.compile(r"\b" + re.escape(str(term).lower()).replace(r"\ ", r"\s+") + r"\b")
            entries.append((str(term).lower(), float(weight), pattern))
    entries.sort(key=lambda item: len(item[0]), reverse=True)
    return entries


def clear_lexicon_cache() -> None:
    _lexicon_terms.cache_clear()


def _context_multiplier(text: str, start: int, end: int) -> float:
    """Applica negazioni e intensificatori nell'intorno del termine trovato."""
    lexicon = get_config().lexicon
    negations = lexicon.get("negazioni", {}) or {}
    boosters = lexicon.get("intensificatori", {}) or {}

    before_tokens = _TOKEN.findall(text[max(0, start - 60) : start].lower())
    after_tokens = _TOKEN.findall(text[end : end + 40].lower())

    multiplier = 1.0
    neg_window = int(negations.get("finestra_parole", 4))
    neg_terms = {str(t).lower() for t in (negations.get("termini", []) or [])}
    if any(tok in neg_terms for tok in before_tokens[-neg_window:]):
        multiplier *= float(negations.get("moltiplicatore", -0.8))

    boost_window = int(boosters.get("finestra_parole", 3))
    boost_terms = {str(t).lower() for t in (boosters.get("termini", []) or [])}
    window = before_tokens[-boost_window:] + after_tokens[:boost_window]
    if any(tok in boost_terms for tok in window):
        multiplier *= float(boosters.get("moltiplicatore", 1.4))
    return multiplier


def detect_themes(text: str) -> List[str]:
    themes_cfg = get_config().lexicon.get("temi", {}) or {}
    lowered = text.lower()
    found = []
    for theme, keywords in themes_cfg.items():
        if any(str(k).lower() in lowered for k in (keywords or [])):
            found.append(theme)
    return found


def _summarize(text: str, matched: List[Dict[str, object]]) -> str:
    """Sintesi in 2 frasi: le due frasi con la maggiore densita' di termini di policy."""
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text.strip()) if len(s.strip()) > 40]
    if not sentences:
        return text.strip()[:280]

    terms = [str(m["termine"]) for m in matched]
    scored = []
    for index, sentence in enumerate(sentences[:60]):
        lowered = sentence.lower()
        hits = sum(1 for term in terms if term in lowered)
        # Le prime frasi di un comunicato contengono di norma la decisione.
        position_bonus = 1.5 if index < 3 else 0.0
        scored.append((hits + position_bonus, index, sentence))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen = sorted(scored[:2], key=lambda item: item[1])
    return " ".join(s[2][:240] for s in chosen)


def classify_text(text: str, title: str = "") -> ToneResult:
    """Classifica un testo di banca centrale con il dizionario bilingue."""
    config = get_config()
    lexicon = config.lexicon
    corpus = f"{title}. {text}" if title else text
    lowered = corpus.lower()

    raw_score = 0.0
    matched: List[Dict[str, object]] = []
    consumed: List[Tuple[int, int]] = []

    for term, weight, pattern in _lexicon_terms():
        for match in pattern.finditer(lowered):
            span = match.span()
            # Evita il doppio conteggio di 'cpi' dentro 'core cpi'.
            if any(span[0] >= s and span[1] <= e for s, e in consumed):
                continue
            consumed.append(span)
            multiplier = _context_multiplier(lowered, span[0], span[1])
            contribution = weight * multiplier
            raw_score += contribution
            matched.append(
                {
                    "termine": term,
                    "peso": round(weight, 2),
                    "moltiplicatore": round(multiplier, 2),
                    "contributo": round(contribution, 2),
                }
            )

    saturation = float(lexicon.get("fattore_saturazione", 8.0)) or 8.0
    tone = 100.0 * math.tanh(raw_score / saturation)

    thresholds = lexicon.get("soglie_tono", {}) or {}
    if tone >= float(thresholds.get("hawkish", 20)):
        label = HAWKISH
    elif tone <= float(thresholds.get("dovish", -20)):
        label = DOVISH
    else:
        label = NEUTRAL

    matched.sort(key=lambda m: abs(float(m["contributo"])), reverse=True)
    themes = detect_themes(corpus)

    summary = _summarize(text or title, matched[:10])
    label_it = {"HAWKISH": "restrittivo", "DOVISH": "accomodante", "NEUTRAL": "neutro"}[label]
    prefix = (
        f"Tono {label_it} ({tone:+.0f}/100)"
        + (f", temi: {', '.join(themes)}." if themes else ".")
        + " "
    )

    return ToneResult(
        tone_score=tone,
        tone_label=label,
        themes=themes,
        summary_it=(prefix + summary)[:600],
        matched_terms=matched[:12],
        classifier="rule_based",
    )
