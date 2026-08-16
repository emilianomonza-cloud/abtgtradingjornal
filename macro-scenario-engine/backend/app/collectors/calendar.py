"""SEZIONE 4.1 — Collector del calendario economico.

Fonte primaria: feed JSON settimanale pubblico di Forex Factory.
Fallback: parser HTML (disattivato di default) e import manuale CSV/JSON.

LIMITE NOTO E DICHIARATO: il feed JSON settimanale espone titolo, valuta, data,
impatto, forecast e previous, ma NON sempre il valore `actual`. Il sistema lo
salva quando c'e'; per gli storici degli `actual` si usa l'import CSV
(POST /api/v1/admin/calendar/import) oppure il parser HTML di fallback.
Questo limite e' mostrato anche nella pagina "Fonti & Salute sistema".
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from ..config import get_config
from ..engine import surprise, taxonomy
from ..storage.models import Event
from ..storage.repositories import record_source_health, upsert_event
from .base import NetworkDisabledError, RateLimitedError, fetcher

logger = logging.getLogger(__name__)

_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ],
        start=1,
    )
}


def _parse_timestamp(raw: Any) -> Optional[datetime]:
    """Converte il campo data del feed in datetime UTC naive."""
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _normalize_row(row: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
    """Normalizza una riga grezza nello schema della tabella `events`."""
    title = str(row.get("title") or row.get("event") or "").strip()
    currency = str(row.get("country") or row.get("currency") or "").strip().upper()
    timestamp = _parse_timestamp(row.get("date") or row.get("timestamp_utc"))
    if not title or not currency or timestamp is None:
        return None

    actual, unit_a = taxonomy.parse_value(row.get("actual"))
    forecast, unit_f = taxonomy.parse_value(row.get("forecast"))
    previous, unit_p = taxonomy.parse_value(row.get("previous"))
    revised, _ = taxonomy.parse_value(row.get("revised"))

    return {
        "timestamp_utc": timestamp,
        "currency": currency,
        "title": title,
        "category": taxonomy.classify_category(title),
        "impact": taxonomy.normalize_impact(row.get("impact")),
        "actual": actual,
        "forecast": forecast,
        "previous": previous,
        "revised": revised,
        "actual_raw": row.get("actual") or None,
        "forecast_raw": row.get("forecast") or None,
        "previous_raw": row.get("previous") or None,
        "revised_raw": row.get("revised") or None,
        "unit": unit_a or unit_f or unit_p,
        "source": source,
    }


# --------------------------------------------------------------------------- #
#  Parser
# --------------------------------------------------------------------------- #


def parse_forexfactory_json(payload: str, source: str) -> List[Dict[str, Any]]:
    """Parser del feed JSON settimanale."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Feed JSON non valido: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("Feed JSON inatteso: attesa una lista di eventi")

    rows: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_row(item, source)
        if normalized is not None:
            rows.append(normalized)
    return rows


def parse_forexfactory_html(payload: str, source: str) -> List[Dict[str, Any]]:
    """Parser HTML di fallback della pagina calendario.

    Best-effort: la struttura della pagina puo' cambiare senza preavviso.
    In caso di fallimento il collector registra la fonte come DEGRADED e il
    sistema prosegue con i fallback: nessun dato viene inventato.
    """
    soup = BeautifulSoup(payload, _html_parser())
    rows: List[Dict[str, Any]] = []
    current_date: Optional[str] = None
    year = datetime.now(timezone.utc).year

    for tr in soup.select("tr.calendar__row, tr.calendar_row"):
        date_cell = tr.select_one(".calendar__date, .date")
        if date_cell and date_cell.get_text(strip=True):
            current_date = date_cell.get_text(" ", strip=True)

        title_cell = tr.select_one(".calendar__event, .event")
        currency_cell = tr.select_one(".calendar__currency, .currency")
        time_cell = tr.select_one(".calendar__time, .time")
        if not (title_cell and currency_cell):
            continue

        impact_cell = tr.select_one(".calendar__impact span, .impact span")
        impact = "YELLOW"
        if impact_cell is not None:
            classes = " ".join(impact_cell.get("class") or [])
            if "red" in classes or "high" in classes:
                impact = "RED"
            elif "ora" in classes or "medium" in classes:
                impact = "ORANGE"

        timestamp = _parse_html_datetime(current_date, time_cell, year)
        if timestamp is None:
            continue

        def cell(selector: str) -> str:
            node = tr.select_one(selector)
            return node.get_text(strip=True) if node else ""

        rows.append(
            {
                "title": title_cell.get_text(" ", strip=True),
                "country": currency_cell.get_text(strip=True),
                "date": timestamp.isoformat(),
                "impact": impact,
                "actual": cell(".calendar__actual, .actual"),
                "forecast": cell(".calendar__forecast, .forecast"),
                "previous": cell(".calendar__previous, .previous"),
            }
        )

    normalized = [_normalize_row(r, source) for r in rows]
    return [r for r in normalized if r is not None]


def _html_parser() -> str:
    """Usa lxml se installato, altrimenti il parser incluso in Python.

    lxml e' piu' veloce ma richiede compilazione su alcune versioni di Python:
    resta opzionale, cosi' l'installazione non puo' fallire per causa sua.
    """
    try:
        import lxml  # noqa: F401
    except ImportError:
        return "html.parser"
    return "lxml"


def _parse_html_datetime(date_text: Optional[str], time_cell, year: int) -> Optional[datetime]:
    """Ricostruisce il timestamp dalle celle 'data' e 'ora' della pagina HTML."""
    if not date_text:
        return None
    match = re.search(r"([A-Za-z]{3})\s*(\d{1,2})", date_text)
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    day = int(match.group(2))

    hour, minute = 0, 0
    raw_time = time_cell.get_text(strip=True) if time_cell else ""
    time_match = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)?", raw_time, re.IGNORECASE)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        meridiem = (time_match.group(3) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def parse_csv(payload: str, source: str = "csv_manuale") -> List[Dict[str, Any]]:
    """Import manuale: CSV con intestazioni.

    Colonne accettate: timestamp_utc|date, currency|country, title|event,
    impact, actual, forecast, previous, revised.
    """
    reader = csv.DictReader(io.StringIO(payload))
    rows: List[Dict[str, Any]] = []
    for raw in reader:
        cleaned = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        normalized = _normalize_row(cleaned, source)
        if normalized is not None:
            rows.append(normalized)
    return rows


# --------------------------------------------------------------------------- #
#  Persistenza
# --------------------------------------------------------------------------- #


def store_rows(session: Session, rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Salva le righe normalizzate e calcola le sorprese sugli eventi rilasciati."""
    created = updated = scored = 0
    stored: List[Event] = []
    for row in rows:
        event, was_created = upsert_event(session, row)
        stored.append(event)
        created += int(was_created)
        updated += int(not was_created)

    # Le sorprese si calcolano dopo il flush: la stima di sigma usa lo storico
    # appena inserito.
    session.flush()
    for event in stored:
        if event.actual is None:
            continue
        surprise.apply_and_store(session, event)
        scored += 1
    return {"creati": created, "aggiornati": updated, "sorprese_calcolate": scored}


async def collect_calendar(session: Session) -> Dict[str, Any]:
    """Esegue i collector di calendario abilitati, in ordine di priorita'."""
    config = get_config()
    sources = sorted(
        [s for s in (config.sources.get("calendario") or []) if s.get("abilitata")],
        key=lambda s: int(s.get("priorita", 99)),
    )

    report: Dict[str, Any] = {"fonti": [], "totale": {"creati": 0, "aggiornati": 0}}

    for spec in sources:
        name = str(spec.get("nome"))
        parser = str(spec.get("parser"))
        label = str(spec.get("etichetta", name))
        urls = [spec.get("url")] + list(spec.get("url_extra") or [])
        urls = [u for u in urls if u]

        if parser == "csv" or not urls:
            record_source_health(
                session,
                name,
                "OK",
                label=label,
                items=0,
                message="Fonte passiva: import manuale via API (nessuna chiamata di rete).",
            )
            continue

        collected: List[Dict[str, Any]] = []
        latency = 0.0
        errors: List[str] = []
        cached_only = True

        for url in urls:
            try:
                result = await fetcher.fetch(url, source=name)
                latency = max(latency, result.latency_ms)
                cached_only = cached_only and result.from_cache
                if parser == "forexfactory_json":
                    collected.extend(parse_forexfactory_json(result.text, name))
                elif parser == "forexfactory_html":
                    collected.extend(parse_forexfactory_html(result.text, name))
                else:
                    errors.append(f"parser sconosciuto: {parser}")
            except (RateLimitedError, NetworkDisabledError) as exc:
                errors.append(str(exc))
            except Exception as exc:  # noqa: BLE001 - la fonte non deve fermare il sistema
                logger.warning("Collector %s fallito su %s: %s", name, url, exc)
                errors.append(f"{url}: {exc}")

        stats = store_rows(session, collected) if collected else {"creati": 0, "aggiornati": 0}

        if collected and not errors:
            status = "OK"
        elif collected:
            status = "DEGRADED"
        else:
            status = "DOWN"

        message = "; ".join(errors)[:900] or (
            "Dati serviti dalla cache locale." if cached_only else "Aggiornamento completato."
        )
        record_source_health(
            session,
            name,
            status,
            label=label,
            latency_ms=latency or None,
            items=len(collected),
            message=message,
        )
        report["fonti"].append(
            {"nome": name, "stato": status, "eventi": len(collected), **stats}
        )
        report["totale"]["creati"] += stats.get("creati", 0)
        report["totale"]["aggiornati"] += stats.get("aggiornati", 0)

    return report


def import_payload(session: Session, payload: str, content_type: str = "csv") -> Dict[str, int]:
    """Import manuale di eventi (fallback definitivo, sempre disponibile)."""
    if content_type == "json":
        data = json.loads(payload)
        if isinstance(data, dict):
            data = data.get("events", [])
        rows = []
        for item in data:
            normalized = _normalize_row(item, "import_manuale")
            if normalized is not None:
                rows.append(normalized)
    else:
        rows = parse_csv(payload, "import_manuale")

    stats = store_rows(session, rows)
    record_source_health(
        session,
        "csv_manuale",
        "OK",
        label="Import manuale CSV / API",
        items=len(rows),
        message=f"Import di {len(rows)} eventi.",
    )
    return stats
