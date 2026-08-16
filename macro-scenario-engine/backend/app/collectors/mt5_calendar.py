"""Import dello storico calendario esportato dal terminale MT5.

PRODUTTORE DEI FILE
-------------------
`mql5/Scripts/CalendarHistoryExporter.mq5` (schema `mt5-calendar-export/1`):
legge il database calendario del terminale MetaTrader 5 tramite l'API pubblica
MQL5 e lo esporta in CSV / JSON / JSONL piu' un report di copertura. E' la
fonte storica PRIMARIA del sistema: profondita' di anni, valori actual e
revisioni inclusi, nessuno scraping.

Questo modulo importa quei file. Accetta i tre formati dati; se l'utente
carica per sbaglio il `*_report.json` lo riconosce e lo dice, invece di
fallire con un errore generico.

SEMANTICA DEI CAMPI (dal produttore, non ipotizzata)
----------------------------------------------------
* I valori numerici sono esportati gia' divisi per 10^6 e arrotondati a
  `digits`; i campi `*_raw` portano l'intero originale del terminale, senza
  perdita. Qui si preferisce SEMPRE il raw: `valore = raw / 1e6`.
* Un valore assente e' `null` in JSON e cella vuota in CSV. Mai zero.
* `time_server` e' nel fuso del server di trading; `time_utc` e' valorizzato
  solo se l'utente ha dichiarato l'offset nell'exporter. Qui si usa `time_utc`
  quando c'e'; altrimenti `time_server`, e il report di import dichiara quante
  righe hanno l'orario non convertito. Un errore di fuso e' al massimo di
  qualche ora: sposta la finestra no-trade, non il giorno dell'evento.
* `importance` e' la tassonomia MetaQuotes (none/low/moderate/high), NON
  l'impatto di Forex Factory. La mappa e' dichiarata qui sotto e nel report.
* `revision` e' il numero di revisione del valore; `revised_previous` e' il
  precedente rivisto. Finiscono in `Event.revised`: lo schema attuale tiene
  l'ultima versione nota, non l'intera storia delle vintage — limite
  dichiarato in docs/audit/02_dataset_identification.md.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..engine import surprise, taxonomy
from ..storage.repositories import record_source_health, upsert_event
from .calendar import store_rows

logger = logging.getLogger(__name__)

SOURCE = "mt5_calendar"
SCHEMA_REPORT = "mt5-calendar-export/1"
VAL_SCALE = 1_000_000.0

# Mappa dichiarata importance MetaQuotes -> impatto interno.
# "none" copre festivita' e voci senza classificazione: GRAY, come le
# festivita' di Forex Factory, cosi' non entrano nelle finestre evento RED.
_IMPORTANCE_MAP = {
    "high": "RED",
    "moderate": "ORANGE",
    "low": "YELLOW",
    "none": "GRAY",
}

# unit/multiplier MetaQuotes -> unita' interna compatta (quella che il resto
# del motore gia' usa: %, K, M, B, T oppure niente).
_MULTIPLIER_MAP = {
    "thousands": "K",
    "millions": "M",
    "billions": "B",
    "trillions": "T",
}


class Mt5ParseError(ValueError):
    """Il file non e' un export mt5-calendar riconoscibile: si dichiara il motivo."""


# --------------------------------------------------------------------------- #
#  Lettura dei tre formati
# --------------------------------------------------------------------------- #


def _parse_iso(text: Any) -> Optional[datetime]:
    if text in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(text).strip())
    except ValueError:
        return None


def _num(value: Any) -> Optional[float]:
    """Numero o None: '' e null sono assenza, mai zero."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _raw_to_value(raw: Any) -> Optional[float]:
    """Intero del terminale (×10^6) -> valore, senza perdita di precisione."""
    n = _num(raw)
    if n is None:
        return None
    return n / VAL_SCALE


def parse_rows(payload: str, filename: str = "") -> List[Dict[str, Any]]:
    """Riconosce CSV / JSON / JSONL dello schema e restituisce i dict grezzi."""
    testo = payload.lstrip("﻿").strip()
    if not testo:
        raise Mt5ParseError("File vuoto.")

    if testo.startswith("{"):
        # Un oggetto singolo in testa: o e' il report di copertura, o e' JSONL.
        try:
            primo = json.loads(testo.splitlines()[0])
        except json.JSONDecodeError:
            primo = None
        if isinstance(primo, dict) and primo.get("schema") == SCHEMA_REPORT:
            raise Mt5ParseError(
                "Questo e' il report di copertura (*_report.json), non i dati: "
                "carica il .csv, il .json o il .jsonl prodotti insieme a lui."
            )
        righe: List[Dict[str, Any]] = []
        for linea in testo.splitlines():
            linea = linea.strip()
            if not linea:
                continue
            try:
                obj = json.loads(linea)
            except json.JSONDecodeError as exc:
                raise Mt5ParseError(f"Riga JSONL non valida: {exc}") from exc
            if isinstance(obj, dict):
                righe.append(obj)
        if not righe:
            raise Mt5ParseError("JSONL senza righe interpretabili.")
        return righe

    if testo.startswith("["):
        try:
            data = json.loads(testo)
        except json.JSONDecodeError as exc:
            raise Mt5ParseError(f"JSON non valido: {exc}") from exc
        if not isinstance(data, list):
            raise Mt5ParseError("Atteso un array JSON di record.")
        return [r for r in data if isinstance(r, dict)]

    # CSV: l'intestazione del produttore inizia con value_id,event_id,...
    prima_riga = testo.splitlines()[0]
    if "value_id" not in prima_riga or "event_id" not in prima_riga:
        raise Mt5ParseError(
            "Il contenuto non sembra un export del CalendarHistoryExporter: "
            "manca l'intestazione con value_id/event_id. "
            f"Prima riga letta: {prima_riga[:120]!r}"
        )
    reader = csv.DictReader(io.StringIO(testo))
    return [dict(r) for r in reader]


# --------------------------------------------------------------------------- #
#  Normalizzazione verso lo schema events
# --------------------------------------------------------------------------- #


def _normalize(
    item: Dict[str, Any], gmt_offset_hours: Optional[float] = None
) -> Tuple[Optional[Dict[str, Any]], Dict[str, int]]:
    """Un record dell'export -> riga per la tabella events + contatori di avviso.

    `gmt_offset_hours`: offset del server del broker dichiarato dall'utente al
    momento dell'import (es. -9.0). Applicato SOLO alle righe senza `time_utc`:
    utc = time_server - offset. E' una correzione a offset fisso, approssimata
    attraverso i cambi di ora legale — dichiarata come tale nel report.
    """
    flags = {
        "fuso_non_dichiarato": 0,
        "fuso_corretto_da_offset": 0,
        "senza_metadati_evento": 0,
        "orario_approssimato": 0,
    }

    titolo = str(item.get("event_name") or "").strip()
    valuta = str(item.get("currency") or "").strip().upper()
    if not titolo or not valuta:
        # Eventi il cui lookup metadati e' fallito nell'exporter: dichiarati
        # nel suo report come event_lookup_failures. Senza titolo e valuta non
        # sono attribuibili: si scartano contandoli, non in silenzio.
        flags["senza_metadati_evento"] = 1
        return None, flags

    quando = _parse_iso(item.get("time_utc"))
    if quando is None:
        quando = _parse_iso(item.get("time_server"))
        if quando is not None:
            if gmt_offset_hours is not None:
                quando = quando - timedelta(hours=gmt_offset_hours)
                flags["fuso_corretto_da_offset"] = 1
            else:
                flags["fuso_non_dichiarato"] = 1
    if quando is None:
        return None, flags

    if str(item.get("time_mode") or "") in ("all_day", "tentative", "no_time"):
        flags["orario_approssimato"] = 1

    # Priorita' ai raw: precisione piena. I campi formattati restano il fallback
    # per export prodotti da versioni che non li includessero.
    actual = _raw_to_value(item.get("actual_raw"))
    if actual is None:
        actual = _num(item.get("actual"))
    forecast = _raw_to_value(item.get("forecast_raw"))
    if forecast is None:
        forecast = _num(item.get("forecast"))
    previous = _raw_to_value(item.get("previous_raw"))
    if previous is None:
        previous = _num(item.get("previous"))
    revised = _raw_to_value(item.get("revised_previous_raw"))
    if revised is None:
        revised = _num(item.get("revised_previous"))

    tipo = str(item.get("event_type") or "")
    importanza = str(item.get("importance") or "").lower()
    impatto = "GRAY" if tipo == "holiday" else _IMPORTANCE_MAP.get(importanza, "YELLOW")

    unita = None
    if str(item.get("unit") or "") == "percent":
        unita = "%"
    else:
        unita = _MULTIPLIER_MAP.get(str(item.get("multiplier") or ""), None)

    row = {
        "timestamp_utc": quando,
        "currency": valuta,
        "title": titolo,
        "category": taxonomy.classify_category(titolo),
        "impact": impatto,
        "actual": actual,
        "forecast": forecast,
        "previous": previous,
        "revised": revised,
        "actual_raw": item.get("actual") or None,
        "forecast_raw": item.get("forecast") or None,
        "previous_raw": item.get("previous") or None,
        "revised_raw": item.get("revised_previous") or None,
        "unit": unita,
        "source": SOURCE,
    }
    return row, flags


def normalize_all(
    items: Iterable[Dict[str, Any]], gmt_offset_hours: Optional[float] = None
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    rows: List[Dict[str, Any]] = []
    totals = {
        "fuso_non_dichiarato": 0,
        "fuso_corretto_da_offset": 0,
        "senza_metadati_evento": 0,
        "orario_approssimato": 0,
    }
    for item in items:
        row, flags = _normalize(item, gmt_offset_hours)
        for k, v in flags.items():
            totals[k] += v
        if row is not None:
            rows.append(row)
    return rows, totals


def coverage_profile(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Profilo di copertura FASE 3, calcolato al momento dell'import.

    Il profiling gira dove girano i dati: questi conteggi finiscono nel report
    dell'import e permettono di confrontare l'archivio con il *_report.json
    dell'exporter (che resta la verita' sulla completezza dell'acquisizione).
    """
    per_anno = Counter(r["timestamp_utc"].year for r in rows)
    per_valuta = Counter(r["currency"] for r in rows)
    per_impatto = Counter(r["impact"] for r in rows)
    per_categoria = Counter(r["category"] for r in rows)
    return {
        "per_anno": dict(sorted(per_anno.items())),
        "per_valuta": dict(per_valuta.most_common()),
        "per_impatto": dict(per_impatto.most_common()),
        "per_categoria": dict(per_categoria.most_common(15)),
        "con_actual": sum(1 for r in rows if r.get("actual") is not None),
        "con_forecast": sum(1 for r in rows if r.get("forecast") is not None),
        "con_revisione": sum(1 for r in rows if r.get("revised") is not None),
    }


# --------------------------------------------------------------------------- #
#  Import
# --------------------------------------------------------------------------- #


# Sopra questa soglia si usa il percorso a lotti: con centinaia di migliaia di
# oggetti nella sessione ORM ogni autoflush riscandaglia l'intera identity map
# e l'import degenera (misurato: >10 minuti per 209k righe, contro ~decine di
# secondi a lotti). Sotto soglia il percorso normale resta identico.
BULK_THRESHOLD = 5_000
BULK_BATCH = 2_000


def _store_rows_bulk(session: Session, rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """Persistenza a lotti per import massivi.

    Correttezza point-in-time della sigma: le righe vengono ordinate per
    timestamp crescente, cosi' quando si calcola la sorpresa di un evento tutto
    il suo passato e' gia' su disco e niente del suo futuro lo e'. Dopo ogni
    lotto la sessione viene svuotata (expunge_all): l'identity map resta
    piccola e il costo per lotto costante.
    """
    rows = sorted(rows, key=lambda r: r["timestamp_utc"])
    created = updated = scored = 0

    for start in range(0, len(rows), BULK_BATCH):
        batch = rows[start : start + BULK_BATCH]
        events = []
        for row in batch:
            event, was_created = upsert_event(session, row)
            events.append(event)
            created += int(was_created)
            updated += int(not was_created)
        session.flush()

        # no_autoflush: le query di storico della sigma non devono rilanciare
        # la ricognizione della sessione a ogni evento.
        with session.no_autoflush:
            for event in events:
                if event.actual is None:
                    continue
                surprise.apply_and_store(session, event)
                scored += 1
        session.flush()
        session.expunge_all()

    return {"creati": created, "aggiornati": updated, "sorprese_calcolate": scored}


def import_mt5(
    session: Session,
    payload: str,
    filename: str = "",
    gmt_offset_hours: Optional[float] = None,
) -> Dict[str, Any]:
    """Importa un export mt5-calendar e registra l'esito nello stato fonti."""
    items = parse_rows(payload, filename)
    rows, warn = normalize_all(items, gmt_offset_hours)

    if not rows:
        stats = {"creati": 0, "aggiornati": 0, "sorprese_calcolate": 0}
    elif len(rows) >= BULK_THRESHOLD:
        stats = _store_rows_bulk(session, rows)
    else:
        stats = store_rows(session, rows)

    istanti = [r["timestamp_utc"] for r in rows]
    periodo = (min(istanti), max(istanti)) if istanti else None

    avvisi: List[str] = []
    if not rows:
        avvisi.append(
            "Nessun evento importabile: il file e' stato letto ma non conteneva "
            "record con titolo, valuta e orario. Nessun dato inventato."
        )
    con_actual = sum(1 for r in rows if r.get("actual") is not None)
    con_forecast = sum(1 for r in rows if r.get("forecast") is not None)
    if rows:
        avvisi.append(
            f"{con_actual} eventi su {len(rows)} hanno il valore effettivo, "
            f"{con_forecast} il consenso: la sorpresa si calcola dove ci sono entrambi."
        )
    if warn["fuso_corretto_da_offset"]:
        avvisi.append(
            f"{warn['fuso_corretto_da_offset']} righe convertite in UTC con "
            f"l'offset dichiarato ({gmt_offset_hours:+.2f}h). E' una correzione a "
            "offset fisso: attraverso i cambi di ora legale del server puo' "
            "sbagliare di un'ora."
        )
    if warn["fuso_non_dichiarato"]:
        avvisi.append(
            f"{warn['fuso_non_dichiarato']} righe usano l'ora del server di trading "
            "perche' time_utc non era valorizzato (offset non dichiarato "
            "nell'exporter). Errore massimo: qualche ora, non il giorno. Puoi "
            "dichiarare l'offset qui nell'import (campo offset) oppure "
            "InpServerGmtOffset nell'exporter."
        )
    if warn["orario_approssimato"]:
        avvisi.append(
            f"{warn['orario_approssimato']} eventi sono All Day / Tentative / senza "
            "orario: la fonte stessa non ne conosce il minuto."
        )
    if warn["senza_metadati_evento"]:
        avvisi.append(
            f"{warn['senza_metadati_evento']} record scartati perche' l'exporter non "
            "aveva risolto i metadati dell'evento (event_lookup_failures nel suo report)."
        )
    avvisi.append(
        "Mappa impatto dichiarata: importance MetaQuotes high->RED, moderate->ORANGE, "
        "low->YELLOW, none/holiday->GRAY. Non e' la scala di Forex Factory."
    )

    record_source_health(
        session,
        SOURCE,
        "OK" if rows else "DEGRADED",
        label="Storico calendario MT5 (import manuale)",
        items=len(rows),
        message=(
            f"Import {filename or 'mt5-calendar'}: {len(rows)} eventi"
            + (f", dal {periodo[0]:%d/%m/%Y} al {periodo[1]:%d/%m/%Y}" if periodo else "")
            + "."
        ),
    )

    return {
        **stats,
        "eventi": len(rows),
        "metodo": "mt5_calendar",
        "periodo": [p.isoformat() for p in periodo] if periodo else None,
        "avvisi": avvisi,
        "copertura": coverage_profile(rows) if rows else None,
    }
