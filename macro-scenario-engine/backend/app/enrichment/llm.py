"""SEZIONE 3.4 — Hook LLM opzionale per l'analisi semantica.

Il layer e' OPZIONALE: se `MSE_LLM_PROVIDER` non e' configurato (o l'SDK non e'
installato, o la chiamata fallisce) il sistema usa il classificatore rule-based,
che resta sempre pienamente funzionante. Nessuna chiave viene mai hardcodata.

Provider supportato: `anthropic` (Claude). Il fallback rule-based copre tutti
gli altri casi: e' una scelta esplicita, non una limitazione nascosta.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from ..config import get_settings
from .rule_based import ToneResult, classify_text, detect_themes

logger = logging.getLogger(__name__)

# Schema dell'output strutturato richiesto al modello.
_TONE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "tono": {
            "type": "integer",
            "description": "Punteggio da -100 (molto accomodante) a +100 (molto restrittivo).",
        },
        "etichetta": {"type": "string", "enum": ["HAWKISH", "NEUTRAL", "DOVISH"]},
        "temi": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "inflazione",
                    "crescita",
                    "occupazione",
                    "rischi",
                    "cambio",
                    "bilancio",
                ],
            },
        },
        "sintesi_it": {
            "type": "string",
            "description": "Sintesi in ESATTAMENTE due frasi, in italiano.",
        },
    },
    "required": ["tono", "etichetta", "temi", "sintesi_it"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "Sei un analista macro specializzato in comunicazione delle banche centrali. "
    "Classifichi il tono di comunicati, minutes e discorsi su una scala "
    "hawkish/dovish e ne estrai i temi. Ti basi solo sul testo fornito: non "
    "aggiungi informazioni esterne e non esprimi previsioni di prezzo. "
    "La sintesi e' in italiano ed e' lunga esattamente due frasi."
)

_MAX_CHARS = 24000


def _client():
    """Costruisce il client Anthropic. Ritorna None se non disponibile."""
    settings = get_settings()
    try:
        import anthropic  # import ritardato: dipendenza opzionale
    except ImportError:
        logger.warning(
            "Provider LLM 'anthropic' configurato ma l'SDK non e' installato "
            "(pip install anthropic). Uso il classificatore rule-based."
        )
        return None

    kwargs: Dict[str, Any] = {"api_key": settings.llm_api_key}
    if settings.llm_base_url:
        kwargs["base_url"] = settings.llm_base_url
    kwargs["timeout"] = float(settings.llm_timeout_seconds)
    return anthropic.Anthropic(**kwargs)


def classify_with_llm(text: str, title: str = "") -> Optional[ToneResult]:
    """Classifica il testo tramite LLM. Ritorna None se non utilizzabile."""
    settings = get_settings()
    if not settings.llm_enabled:
        return None
    if settings.llm_provider.lower() != "anthropic":
        logger.warning(
            "Provider LLM '%s' non implementato: uso il classificatore rule-based.",
            settings.llm_provider,
        )
        return None

    client = _client()
    if client is None:
        return None

    body = (text or "").strip()[:_MAX_CHARS]
    if not body:
        return None

    prompt = (
        f"Titolo: {title}\n\n"
        f"Testo del documento:\n---\n{body}\n---\n\n"
        "Classifica il tono complessivo sulla scala hawkish/dovish, elenca i temi "
        "trattati e scrivi una sintesi di due frasi in italiano."
    )

    try:
        response = client.messages.create(
            model=settings.llm_model,
            max_tokens=16000,
            system=_SYSTEM_PROMPT,
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": _TONE_SCHEMA},
            },
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # rete, quota, credenziali, timeout...
        logger.warning("Chiamata LLM fallita (%s): fallback rule-based.", exc)
        return None

    if response.stop_reason == "refusal":
        logger.warning("Il modello ha rifiutato la richiesta: fallback rule-based.")
        return None

    payload_text = next(
        (block.text for block in response.content if block.type == "text"), ""
    )
    try:
        data = json.loads(payload_text)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Risposta LLM non interpretabile: fallback rule-based.")
        return None

    try:
        tone = max(-100.0, min(100.0, float(data["tono"])))
        label = str(data["etichetta"]).upper()
        themes = [str(t) for t in (data.get("temi") or [])]
        summary = str(data.get("sintesi_it", "")).strip()
    except (KeyError, TypeError, ValueError):
        logger.warning("Campi mancanti nella risposta LLM: fallback rule-based.")
        return None

    if label not in ("HAWKISH", "NEUTRAL", "DOVISH"):
        label = "NEUTRAL"
    if not themes:
        themes = detect_themes(f"{title} {body}")

    return ToneResult(
        tone_score=tone,
        tone_label=label,
        themes=themes,
        summary_it=summary[:600],
        matched_terms=[],
        classifier=f"llm:{settings.llm_model}",
    )


def classify(text: str, title: str = "") -> ToneResult:
    """Punto d'ingresso unico: LLM se configurato, altrimenti rule-based."""
    result = classify_with_llm(text, title)
    if result is not None:
        return result
    return classify_text(text, title)
