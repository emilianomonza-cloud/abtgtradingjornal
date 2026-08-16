"""SEZIONE 4.3 — Collector dei documenti delle banche centrali (RSS ufficiali).

Il parsing dei feed usa `xml.etree.ElementTree`, incluso in Python: nessuna
dipendenza da compilare, quindi l'installazione funziona anche sulle versioni
di Python piu' recenti.

LIMITE NOTO E DICHIARATO: viene scaricato SOLO il feed RSS di ogni banca
(una richiesta per fonte per ciclo). Il testo analizzato e' quindi titolo +
sommario del feed, non il documento integrale: scaricare ogni singolo PDF/HTML
significherebbe decine di richieste per ciclo, in contrasto con il rate limiting
richiesto e con i ToS delle fonti. La conseguenza — analisi semantica su testo
sintetico — e' segnalata in dashboard e riduce la confidenza degli scenari.
"""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..config import get_config
from ..enrichment.service import upsert_document
from ..storage.repositories import record_source_health
from .base import NetworkDisabledError, RateLimitedError, fetcher

logger = logging.getLogger(__name__)

_MINUTES_HINTS = ("minutes", "account of", "resoconto")
_SPEECH_HINTS = ("speech", "remarks", "discorso", "intervento", "lecture")

_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")

# Nomi dei tag da cercare, senza namespace: i feed Atom e RSS usano nomi diversi.
_TITLE_TAGS = ("title",)
_LINK_TAGS = ("link", "guid", "id")
_BODY_TAGS = ("description", "summary", "encoded", "content")
_DATE_TAGS = ("pubDate", "published", "updated", "date")


def _local_name(tag: str) -> str:
    """'{http://www.w3.org/2005/Atom}entry' -> 'entry'."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _strip_html(text: str) -> str:
    """Rimuove i tag HTML che i feed inseriscono dentro description/summary."""
    return _SPACES.sub(" ", _TAGS.sub(" ", html.unescape(text or ""))).strip()


def _child_text(item: ET.Element, names: tuple[str, ...]) -> str:
    for child in item:
        if _local_name(child.tag) in names:
            text = (child.text or "").strip()
            if text:
                return text
    return ""


def _extract_link(item: ET.Element) -> str:
    """RSS mette l'URL nel testo di <link>, Atom nell'attributo href."""
    for child in item:
        if _local_name(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        if href:
            return href
        text = (child.text or "").strip()
        if text:
            return text
    return _child_text(item, ("guid", "id"))


def _extract_body(item: ET.Element) -> str:
    parts: List[str] = []
    for child in item:
        if _local_name(child.tag) in _BODY_TAGS:
            parts.append(_strip_html(child.text or ""))
    return " ".join(p for p in parts if p).strip()


def _parse_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    text = raw.strip()
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _refine_doc_type(title: str, default: str) -> str:
    lowered = title.lower()
    if any(h in lowered for h in _MINUTES_HINTS):
        return "MINUTES"
    if any(h in lowered for h in _SPEECH_HINTS):
        return "SPEECH"
    return default


def parse_feed(payload: str, spec: Dict[str, Any], limit: int = 25) -> List[Dict[str, Any]]:
    """Estrae gli item da un feed RSS 2.0 o Atom."""
    try:
        root = ET.fromstring(payload.strip())
    except ET.ParseError as exc:
        raise ValueError(f"Feed XML non valido: {exc}") from exc

    items = [el for el in root.iter() if _local_name(el.tag) in ("item", "entry")]

    documents: List[Dict[str, Any]] = []
    for item in items[:limit]:
        title = _child_text(item, _TITLE_TAGS)
        link = _extract_link(item)
        if not title or not link:
            continue

        body = _extract_body(item)
        published = _parse_date(_child_text(item, _DATE_TAGS))

        documents.append(
            {
                "url": link,
                "bank": str(spec.get("banca", "")),
                "currency": str(spec.get("valuta", "")),
                "doc_type": _refine_doc_type(
                    title, str(spec.get("tipo_documento", "PRESS_RELEASE"))
                ),
                "title": title,
                "published_at": published,
                "body": f"{title}. {body}".strip(),
                "source": str(spec.get("nome", "rss")),
            }
        )
    return documents


async def collect_central_banks(session: Session) -> Dict[str, Any]:
    """Aggiorna i documenti di tutte le banche centrali abilitate."""
    config = get_config()
    specs = [s for s in (config.sources.get("banche_centrali") or []) if s.get("abilitata")]

    report: Dict[str, Any] = {"fonti": [], "nuovi_documenti": 0}

    for spec in specs:
        name = str(spec.get("nome"))
        label = str(spec.get("etichetta", name))
        url = str(spec.get("url", ""))
        if not url:
            continue

        status = "OK"
        message = ""
        latency: Optional[float] = None
        documents: List[Dict[str, Any]] = []

        try:
            result = await fetcher.fetch(url, source=name, cache_ttl_seconds=1800)
            latency = result.latency_ms or None
            documents = parse_feed(result.text, spec)
            if not documents:
                status = "DEGRADED"
                message = "Feed raggiunto ma nessun documento riconosciuto."
            elif result.from_cache:
                message = "Dati serviti dalla cache locale."
        except (RateLimitedError, NetworkDisabledError) as exc:
            status = "DEGRADED"
            message = str(exc)
        except Exception as exc:  # noqa: BLE001 - una banca giu' non ferma il sistema
            logger.warning("Collector %s fallito: %s", name, exc)
            status = "DOWN"
            message = str(exc)

        created = 0
        for doc in documents:
            try:
                _, was_created = upsert_document(session, doc)
                created += int(was_created)
            except Exception:  # pragma: no cover - difensivo
                logger.exception("Errore nel salvataggio del documento %s", doc.get("url"))

        record_source_health(
            session,
            name,
            status,
            label=label,
            latency_ms=latency,
            items=len(documents),
            message=message[:900] or f"{created} nuovi documenti analizzati.",
        )
        report["fonti"].append(
            {"nome": name, "stato": status, "documenti": len(documents), "nuovi": created}
        )
        report["nuovi_documenti"] += created

    return report
