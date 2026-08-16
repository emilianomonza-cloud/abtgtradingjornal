"""Layer semantico: classificazione hawkish/dovish di comunicati e discorsi."""

from .llm import classify, classify_with_llm  # noqa: F401
from .rule_based import (  # noqa: F401
    DOVISH,
    HAWKISH,
    NEUTRAL,
    ToneResult,
    classify_text,
    clear_lexicon_cache,
    detect_themes,
)
from .service import analyze, reclassify_all, upsert_document  # noqa: F401
