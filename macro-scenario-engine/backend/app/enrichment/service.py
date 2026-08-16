"""Orchestrazione del layer semantico: classifica e storicizza i documenti."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..storage.models import CBDocument
from ..storage.repositories import naive_utc, now_utc
from .llm import classify
from .rule_based import ToneResult

logger = logging.getLogger(__name__)


def analyze(text: str, title: str = "") -> ToneResult:
    """Classifica un testo (LLM se configurato, altrimenti rule-based)."""
    return classify(text, title)


def upsert_document(session: Session, data: Dict[str, Any]) -> tuple[CBDocument, bool]:
    """Inserisce o aggiorna un documento di banca centrale e lo classifica.

    La deduplica e' sull'URL: un documento gia' analizzato non viene
    riclassificato (risparmia chiamate LLM e mantiene stabile lo storico).
    """
    url = str(data["url"])
    doc = session.execute(
        select(CBDocument).where(CBDocument.url == url)
    ).scalar_one_or_none()

    created = False
    if doc is None:
        doc = CBDocument(url=url)
        session.add(doc)
        created = True
    elif doc.body:
        # Gia' analizzato: aggiorno solo i metadati leggeri.
        doc.title = str(data.get("title", doc.title))[:512]
        return doc, False

    published: Optional[datetime] = data.get("published_at")
    doc.bank = str(data.get("bank", "")).upper()[:16]
    doc.currency = str(data.get("currency", "")).upper()[:8]
    doc.doc_type = str(data.get("doc_type", "PRESS_RELEASE"))[:24]
    doc.title = str(data.get("title", ""))[:512]
    doc.published_at = naive_utc(published) if published else now_utc()
    doc.body = str(data.get("body", ""))[:200000]
    doc.source = str(data.get("source", "rss"))[:48]
    doc.collected_at = now_utc()

    result = analyze(doc.body, doc.title)
    doc.tone_score = result.tone_score
    doc.tone_label = result.tone_label
    doc.themes = result.themes
    doc.summary_it = result.summary_it
    doc.classifier = result.classifier
    doc.matched_terms = result.matched_terms

    return doc, created


def reclassify_all(session: Session, limit: int = 200) -> int:
    """Riclassifica i documenti gia' archiviati (dopo un cambio di dizionario)."""
    documents = list(
        session.execute(
            select(CBDocument).order_by(CBDocument.published_at.desc()).limit(limit)
        )
        .scalars()
        .all()
    )
    count = 0
    for doc in documents:
        if not doc.body:
            continue
        result = analyze(doc.body, doc.title)
        doc.tone_score = result.tone_score
        doc.tone_label = result.tone_label
        doc.themes = result.themes
        doc.summary_it = result.summary_it
        doc.classifier = result.classifier
        doc.matched_terms = result.matched_terms
        count += 1
    logger.info("Riclassificati %d documenti", count)
    return count
