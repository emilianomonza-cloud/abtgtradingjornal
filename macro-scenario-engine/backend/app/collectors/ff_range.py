"""Import dello storico calendario da una pagina Forex Factory con `?range=`.

PERCHE' QUESTO MODULO ESISTE
----------------------------
Il feed JSON pubblico di Forex Factory copre solo la settimana corrente: non
esiste un archivio storico pubblico, e l'export CSV del sito richiede un account
registrato. Lo storico pero' e' visibile a chiunque apra una pagina come:

    https://www.forexfactory.com/calendar?range=aug1.2019-oct2.2019

La strada scelta e' quindi: **l'utente apre la pagina nel proprio browser** (una
pagina che ha tutto il diritto di consultare) e ne estrae i dati con lo snippet
`estrai_forexfactory.js`, che gira in locale e non fa nessuna richiesta di rete.
Il file prodotto viene poi importato qui.

Nessuno scraping automatico, nessun aggiramento del login, nessuna richiesta
ripetuta ai server della fonte: il traffico e' quello di una persona che legge
una pagina. E' il fallback dichiarato nel README, non una scorciatoia.

Il modulo accetta due formati, entrambi con la stessa fedelta':

  1. **estrazione JSON** — il file prodotto dallo snippet.
  2. **HTML salvato con Ctrl+S** — si legge lo stato JavaScript
     `calendarComponentStates`, incorporato in uno `<script>` della pagina.

Entrambi portano il campo `dateline`/`ts`, cioe' un **timestamp UNIX assoluto**.
Il fuso impostato sul sito cambia solo le etichette visibili, non i timestamp:
non serve quindi impostare GMT prima di salvare.

LA TABELLA HTML NON VIENE LETTA IN AUTOMATICO, DI PROPOSITO
------------------------------------------------------------
Misurato sulla pagina reale: la tabella e' renderizzata in modo pigro e in una
pagina salvata sono presenti circa **52 righe su 801 eventi**. Un parser della
tabella importerebbe il 7% dei dati dichiarando "importato con successo": e'
peggio di un errore, perche' l'errore lo vedi e una sigma calcolata sul 7% del
campione no. Si aggiungano due difetti minori: i titoli seguono la lingua
dell'interfaccia (quindi non sono confrontabili fra file) e la riga di
intestazione del giorno non porta l'anno, che andrebbe indovinato.

`parse_table_html` resta disponibile per chi la richieda esplicitamente
(`consenti_tabella=True`), e in quel caso il report lo dichiara a caratteri
cubitali. Non parte mai da sola.

LIMITE DICHIARATO: la struttura della pagina appartiene alla fonte e puo'
cambiare senza preavviso. Quando cambia, il parser NON indovina: restituisce
zero righe e un motivo leggibile, che finisce nel report e nella pagina Fonti.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..storage.repositories import record_source_health
from .calendar import _MONTHS, _normalize_row, _html_parser, store_rows

logger = logging.getLogger(__name__)

SOURCE = "forexfactory_storico"

# Classi di impatto usate dal sito, mappate sulla scala interna.
_IMPACT_CLASSES = (
    ("red", "RED"),
    ("ora", "ORANGE"),
    ("yel", "YELLOW"),
    ("gra", "GRAY"),
    ("holiday", "GRAY"),
)

# La pagina non e' un'unica struttura garantita: gli stessi campi sono comparsi
# nel tempo con nomi diversi. Si prova in ordine e si prende il primo presente,
# invece di affidarsi a un nome solo che domani potrebbe non esserci.
_KEYS_TITLE = ("name", "title", "event", "eventName", "prefixedName")
_KEYS_CURRENCY = ("currency", "country", "ccy")
_KEYS_ACTUAL = ("actual", "actualValue")
_KEYS_FORECAST = ("forecast", "forecastValue", "consensus")
_KEYS_PREVIOUS = ("previous", "previousValue")
_KEYS_REVISED = ("revision", "revised", "revisedPrevious", "previousRevised")
_KEYS_IMPACT = ("impactClass", "impactName", "impactTitle", "impact")
# `dateline` e' il nome usato dallo stato interno del sito; `ts` quello usato
# dall'estrattore, che lo ricopia. Entrambi sono secondi UNIX assoluti.
_KEYS_DATELINE = ("dateline", "ts", "timestamp", "unix", "date_unix")
_KEYS_ISO = ("utc", "timestamp_utc", "date", "datetime", "dateISO")
_KEYS_TIME = ("timeLabel", "time", "timeMasked")
# Eventi "All Day" / "Tentative": la fonte stessa non conosce l'orario esatto.
_KEYS_ORARIO_APPROSSIMATO = ("timeApprox", "timeMasked")

# Nomi con cui i vari formati chiamano la lista degli eventi e il metodo usato.
_KEYS_LISTA = ("eventi", "events")
_METODI = {
    "dom-state": "stato",
    "stato": "stato",
    "merged": "unione",  # piu' file uniti prima dell'import
    "tabella": "tabella",
    "table": "tabella",
}


class RangeParseError(ValueError):
    """La pagina non contiene i dati attesi: si dichiara, non si indovina."""


# --------------------------------------------------------------------------- #
#  Utilita'
# --------------------------------------------------------------------------- #


def _pick(item: Dict[str, Any], keys: Iterable[str]) -> Any:
    """Primo valore non vuoto fra le chiavi candidate."""
    for key in keys:
        if key in item:
            value = item[key]
            if value not in (None, "", []):
                return value
    return None


def _clean_text(raw: Any) -> str:
    """Testo senza tag HTML: alcune celle arrivano con markup incorporato."""
    if raw is None:
        return ""
    return re.sub(r"<[^>]+>", " ", str(raw)).replace("&nbsp;", " ").strip()


def _impact_from(raw: Any) -> str:
    text = str(raw or "").lower()
    for needle, level in _IMPACT_CLASSES:
        if needle in text:
            return level
    if "high" in text:
        return "RED"
    if "medium" in text:
        return "ORANGE"
    if "low" in text:
        return "YELLOW"
    return "YELLOW"


def parse_range_param(url_or_range: str) -> Optional[Tuple[datetime, datetime]]:
    """Legge `range=aug1.2019-oct2.2019` e restituisce (inizio, fine).

    Serve soprattutto a conoscere l'ANNO: nella tabella renderizzata la cella
    data riporta solo "Thu Aug 1", senza anno. Senza questo l'import di uno
    storico finirebbe silenziosamente nell'anno corrente.
    """
    match = re.search(
        r"range=([a-z]{3})(\d{1,2})\.(\d{4})-([a-z]{3})(\d{1,2})\.(\d{4})",
        str(url_or_range),
        re.IGNORECASE,
    )
    if not match:
        return None
    m1, d1, y1, m2, d2, y2 = match.groups()
    month1 = _MONTHS.get(m1.lower())
    month2 = _MONTHS.get(m2.lower())
    if month1 is None or month2 is None:
        return None
    try:
        return (
            datetime(int(y1), month1, int(d1)),
            datetime(int(y2), month2, int(d2)),
        )
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
#  1. Estrazione prodotta dallo snippet del browser
# --------------------------------------------------------------------------- #


def parse_extraction(payload: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Legge il JSON prodotto dall'estrattore che gira nel browser.

    Sono accettati due schemi, perche' l'estrattore e' un file che l'utente puo'
    tenere aggiornato per conto suo: quello che conta e' la sostanza, non il
    nome dei campi.

        {"schema": "ff-calendar/1",  ... "events": [{"ts": ..., "title": ...}]}
        {"schema": "mse-ff-export/1", ... "eventi": [{"dateline": ..., "name": ...}]}

    Una lista nuda di eventi viene accettata allo stesso modo.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RangeParseError(f"File di estrazione non valido: {exc}") from exc

    if isinstance(data, list):
        items, data = data, {}
    elif isinstance(data, dict):
        items = _pick(data, _KEYS_LISTA)
        if items is None and isinstance(data.get("days"), list):
            # Stato grezzo del sito salvato cosi' com'e': days[].events[].
            items = [
                e
                for giorno in data["days"]
                if isinstance(giorno, dict)
                for e in (giorno.get("events") or [])
            ]
        if items is None:
            raise RangeParseError(
                "File JSON senza lista di eventi (attesa sotto 'events', 'eventi' "
                "oppure 'days'): chiavi trovate: "
                + ", ".join(list(data.keys())[:10])
            )
    else:
        raise RangeParseError("File di estrazione di tipo inatteso.")

    if not isinstance(items, list):
        raise RangeParseError("Il campo degli eventi non contiene una lista.")

    meta = {
        "schema": data.get("schema"),
        "range": data.get("range"),
        "metodo": _METODI.get(str(data.get("metodo") or data.get("method") or "").lower(), "stato"),
        "fuso": data.get("fuso"),
        "url": data.get("sourceUrl") or data.get("url"),
        "eventi_grezzi": len(items),
        "scartati_dall_estrattore": data.get("skipped"),
    }
    rows = _rows_from_items(items, fallback_year=None)
    meta["orario_approssimato"] = sum(
        1 for i in items if isinstance(i, dict) and _pick(i, _KEYS_ORARIO_APPROSSIMATO)
    )
    return rows, meta


# --------------------------------------------------------------------------- #
#  2. HTML con lo stato JavaScript
# --------------------------------------------------------------------------- #


def extract_state_blocks(html: str) -> List[Any]:
    """Estrae gli oggetti `calendarComponentStates[...] = {...}` dalla pagina.

    Il confine dell'oggetto si trova bilanciando le parentesi graffe (ignorando
    quelle dentro le stringhe): una regex non basta, l'oggetto e' annidato.
    """
    blocks: List[Any] = []
    for match in re.finditer(r"calendarComponentStates\s*\[[^\]]*\]\s*=\s*", html):
        start = html.find("{", match.end())
        if start == -1:
            continue
        end = _matching_brace(html, start)
        if end is None:
            continue
        raw = html[start : end + 1]
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError:
            # Lo stato e' JavaScript, non JSON puro: puo' contenere chiavi senza
            # virgolette o virgole finali. Si tenta una normalizzazione minima.
            try:
                blocks.append(json.loads(_js_to_json(raw)))
            except json.JSONDecodeError as exc:
                logger.warning("Stato calendario non interpretabile: %s", exc)
    return blocks


def _matching_brace(text: str, start: int) -> Optional[int]:
    depth = 0
    in_string = False
    quote = ""
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_string = False
            continue
        if ch in "\"'":
            in_string = True
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _js_to_json(raw: str) -> str:
    """Normalizzazione minima di un oggetto JavaScript verso JSON."""
    text = re.sub(r"([{,]\s*)([A-Za-z_$][A-Za-z0-9_$]*)\s*:", r'\1"\2":', raw)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text


def _iter_state_events(block: Any) -> Iterable[Dict[str, Any]]:
    """Percorre lo stato e restituisce ogni dizionario che sembra un evento.

    Non si assume la profondita' dell'annidamento (`days[].events[]` e' la forma
    piu' comune ma non l'unica vista): si scende ricorsivamente e si riconosce
    un evento dal fatto che porta un titolo e una valuta.
    """
    if isinstance(block, dict):
        if _pick(block, _KEYS_TITLE) and _pick(block, _KEYS_CURRENCY):
            yield block
            return
        for value in block.values():
            yield from _iter_state_events(value)
    elif isinstance(block, list):
        for value in block:
            yield from _iter_state_events(value)


def parse_state_html(html: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Percorso preferito sull'HTML: legge lo stato JavaScript della pagina."""
    blocks = extract_state_blocks(html)
    if not blocks:
        raise RangeParseError("Nessuno stato 'calendarComponentStates' nella pagina.")

    items: List[Dict[str, Any]] = []
    for block in blocks:
        items.extend(_iter_state_events(block))
    if not items:
        raise RangeParseError("Stato calendario presente ma senza eventi riconoscibili.")

    periodo = parse_range_param(html[:4000])
    rows = _rows_from_items(items, fallback_year=periodo[0].year if periodo else None)
    return rows, {"metodo": "stato", "eventi_grezzi": len(items)}


# --------------------------------------------------------------------------- #
#  3. HTML renderizzato (ultima risorsa)
# --------------------------------------------------------------------------- #


def parse_table_html(html: str, year: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Legge la tabella renderizzata riga per riga.

    Le celle data sono valorizzate solo sulla prima riga di ogni giornata: si
    trascina l'ultima vista. L'anno non compare nella tabella e va passato,
    altrimenti uno storico verrebbe datato all'anno corrente.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, _html_parser())
    righe = soup.select("tr.calendar__row, tr.calendar_row")
    if not righe:
        raise RangeParseError("Nessuna riga 'calendar__row' nella pagina.")

    periodo = parse_range_param(html[:4000])
    anno = year or (periodo[0].year if periodo else datetime.now(timezone.utc).year)
    mese_precedente: Optional[int] = None

    raw_rows: List[Dict[str, Any]] = []
    data_corrente: Optional[str] = None

    for tr in righe:
        cella_data = tr.select_one(".calendar__date, .date")
        if cella_data is not None and cella_data.get_text(strip=True):
            data_corrente = cella_data.get_text(" ", strip=True)

        titolo = tr.select_one(".calendar__event, .event")
        valuta = tr.select_one(".calendar__currency, .currency")
        if not (titolo and valuta and data_corrente):
            continue

        match = re.search(r"([A-Za-z]{3})\s*(\d{1,2})", data_corrente)
        if not match:
            continue
        mese = _MONTHS.get(match.group(1).lower())
        if mese is None:
            continue
        # Un range puo' scavalcare il capodanno: quando il mese torna indietro
        # rispetto alla riga precedente si e' entrati nell'anno successivo.
        if mese_precedente is not None and mese < mese_precedente:
            anno += 1
        mese_precedente = mese

        istante = _combina_data_ora(anno, mese, int(match.group(2)), tr)
        if istante is None:
            continue

        def cella(selector: str) -> str:
            node = tr.select_one(selector)
            return node.get_text(strip=True) if node else ""

        impatto_node = tr.select_one(".calendar__impact span, .impact span")
        impatto = _impact_from(" ".join(impatto_node.get("class") or []) if impatto_node else "")

        raw_rows.append(
            {
                "title": titolo.get_text(" ", strip=True),
                "country": valuta.get_text(strip=True),
                "date": istante.isoformat(),
                "impact": impatto,
                "actual": cella(".calendar__actual, .actual"),
                "forecast": cella(".calendar__forecast, .forecast"),
                "previous": cella(".calendar__previous, .previous"),
            }
        )

    rows = [r for r in (_normalize_row(r, SOURCE) for r in raw_rows) if r is not None]
    return rows, {"metodo": "tabella", "anno_iniziale": anno, "eventi_grezzi": len(raw_rows)}


def _combina_data_ora(anno: int, mese: int, giorno: int, tr) -> Optional[datetime]:
    """Costruisce il timestamp dalla data della giornata e dall'ora della riga.

    Le righe senza orario numerico ("All Day", "Tentative", "Day 1") vengono
    datate a mezzanotte: l'informazione utile e' il giorno, non un orario che la
    fonte stessa non conosce.
    """
    ora, minuti = 0, 0
    cella_ora = tr.select_one(".calendar__time, .time")
    testo = cella_ora.get_text(strip=True) if cella_ora else ""
    match = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)?", testo, re.IGNORECASE)
    if match:
        ora = int(match.group(1))
        minuti = int(match.group(2))
        meridiem = (match.group(3) or "").lower()
        if meridiem == "pm" and ora < 12:
            ora += 12
        elif meridiem == "am" and ora == 12:
            ora = 0
    try:
        return datetime(anno, mese, giorno, ora, minuti)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
#  Normalizzazione comune
# --------------------------------------------------------------------------- #


def _rows_from_items(
    items: Iterable[Dict[str, Any]], fallback_year: Optional[int]
) -> List[Dict[str, Any]]:
    """Converte gli eventi grezzi nello schema della tabella `events`."""
    rows: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        istante = _timestamp_from_item(item, fallback_year)
        if istante is None:
            continue
        grezza = {
            "title": _clean_text(_pick(item, _KEYS_TITLE)),
            "country": _clean_text(_pick(item, _KEYS_CURRENCY)).upper(),
            "date": istante.isoformat(),
            "impact": _impact_from(_pick(item, _KEYS_IMPACT)),
            "actual": _clean_text(_pick(item, _KEYS_ACTUAL)),
            "forecast": _clean_text(_pick(item, _KEYS_FORECAST)),
            "previous": _clean_text(_pick(item, _KEYS_PREVIOUS)),
            "revised": _clean_text(_pick(item, _KEYS_REVISED)),
        }
        normalizzata = _normalize_row(grezza, SOURCE)
        if normalizzata is not None:
            rows.append(normalizzata)
    return rows


def _timestamp_from_item(item: Dict[str, Any], fallback_year: Optional[int]) -> Optional[datetime]:
    """Ricava l'istante UTC dell'evento.

    Priorita' al timestamp UNIX: e' assoluto e non dipende dal fuso orario
    impostato sul sito, che e' la principale fonte di errore silenzioso in
    questo tipo di import.
    """
    dateline = _pick(item, _KEYS_DATELINE)
    if dateline is not None:
        try:
            secondi = int(float(dateline))
        except (TypeError, ValueError):
            secondi = 0
        if secondi > 0:
            # Millisecondi: alcune estrazioni li riportano in ms.
            if secondi > 10_000_000_000:
                secondi //= 1000
            return datetime.fromtimestamp(secondi, tz=timezone.utc).replace(tzinfo=None)

    # Nessun UNIX: si accetta una data ISO gia' pronta.
    for key in _KEYS_ISO:
        value = item.get(key)
        if not value:
            continue
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        if parsed.year < 1990 and fallback_year:
            parsed = parsed.replace(year=fallback_year)
        return parsed
    return None


# --------------------------------------------------------------------------- #
#  Ingresso unico
# --------------------------------------------------------------------------- #


def _sembra_mhtml(testo: str) -> bool:
    """Riconosce il salvataggio "Pagina singola" di Chrome/Edge.

    E' un archivio MHTML, non HTML: il contenuto e' codificato e lo stato non si
    trova. Senza questo controllo l'utente riceverebbe un generico "stato non
    trovato" e non saprebbe che deve solo cambiare voce nel menu di salvataggio.
    """
    testa = testo[:2000]
    return bool(
        re.match(r"\s*(From|MIME-Version|Content-Type):", testa, re.IGNORECASE)
        and re.search(r"multipart/related", testa, re.IGNORECASE)
    )


def parse_any(
    payload: str, year: Optional[int] = None, consenti_tabella: bool = False
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Riconosce da solo il formato: estrazione JSON oppure stato JavaScript.

    La tabella renderizzata NON entra in questo percorso automatico: importerebbe
    una frazione degli eventi facendo credere di aver funzionato. Va richiesta
    esplicitamente con `consenti_tabella=True`.
    """
    testo = payload.strip()
    tentativi: List[str] = []

    if testo.startswith("{") or testo.startswith("["):
        return parse_extraction(testo)

    if _sembra_mhtml(testo):
        raise RangeParseError(
            "Il file e' un archivio MHTML, non HTML. In Chrome salva con Ctrl+S "
            'scegliendo "Pagina web, completa" oppure "Solo HTML", non "Singolo file".'
        )

    try:
        return parse_state_html(testo)
    except RangeParseError as exc:
        tentativi.append(f"stato JavaScript: {exc}")

    if consenti_tabella:
        try:
            rows, meta = parse_table_html(testo, year=year)
            meta["tentativi_precedenti"] = tentativi
            return rows, meta
        except RangeParseError as exc:
            tentativi.append(f"tabella renderizzata: {exc}")

    sembra_ff = "forexfactory" in testo.lower() or "calendar__row" in testo
    dettaglio = (
        "E' una pagina Forex Factory, ma lo stato 'calendarComponentStates' non c'e': "
        "probabilmente e' stata salvata prima del caricamento completo, oppure il sito "
        "ha cambiato struttura. La tabella HTML non viene usata come ripiego perche' "
        "nel file salvato ne compare solo una frazione delle righe."
        if sembra_ff
        else "Non sembra una pagina del calendario di Forex Factory."
    )
    raise RangeParseError(
        f"Nessun parser ha riconosciuto il contenuto. {dettaglio} "
        "Dettaglio tecnico: " + " | ".join(tentativi)
    )


def import_range(
    session: Session,
    payload: str,
    year: Optional[int] = None,
    consenti_tabella: bool = False,
) -> Dict[str, Any]:
    """Importa lo storico e registra l'esito nello stato fonti."""
    rows, meta = parse_any(payload, year=year, consenti_tabella=consenti_tabella)
    stats = store_rows(session, rows) if rows else {"creati": 0, "aggiornati": 0}

    periodo = _periodo(rows)
    messaggio = (
        f"Import storico ({meta.get('metodo') or 'estrazione'}): {len(rows)} eventi"
        + (f", dal {periodo[0]:%d/%m/%Y} al {periodo[1]:%d/%m/%Y}" if periodo else "")
        + "."
    )
    if meta.get("metodo") == "tabella":
        messaggio += (
            " Orari dedotti dalla tabella: dipendono dal fuso impostato sul sito."
        )
    record_source_health(
        session,
        SOURCE,
        "OK" if rows else "DEGRADED",
        label="Storico Forex Factory (import manuale)",
        items=len(rows),
        message=messaggio,
    )

    return {
        **stats,
        "eventi": len(rows),
        "metodo": meta.get("metodo") or "estrazione",
        "periodo": [p.isoformat() for p in periodo] if periodo else None,
        "meta": meta,
        "avvisi": _avvisi(rows, meta),
    }


def _periodo(rows: List[Dict[str, Any]]) -> Optional[Tuple[datetime, datetime]]:
    istanti = [r["timestamp_utc"] for r in rows if r.get("timestamp_utc")]
    return (min(istanti), max(istanti)) if istanti else None


def _avvisi(rows: List[Dict[str, Any]], meta: Dict[str, Any]) -> List[str]:
    """Cosa l'utente deve sapere di questo import, detto esplicitamente."""
    avvisi: List[str] = []
    if not rows:
        avvisi.append(
            "Nessun evento riconosciuto: la struttura della pagina potrebbe essere "
            "cambiata. Nessun dato e' stato inventato per riempire il vuoto."
        )
        return avvisi

    con_actual = sum(1 for r in rows if r.get("actual") is not None)
    con_forecast = sum(1 for r in rows if r.get("forecast") is not None)
    avvisi.append(
        f"{con_actual} eventi su {len(rows)} hanno il valore effettivo, "
        f"{con_forecast} hanno il consenso: la sorpresa si calcola solo dove ci sono entrambi."
    )
    if meta.get("metodo") == "tabella":
        avvisi.append(
            "IMPORT PARZIALE, QUASI CERTAMENTE INCOMPLETO. Letto dalla tabella "
            "renderizzata su richiesta esplicita: la tabella e' caricata in modo "
            "pigro e in una pagina salvata ne compare solo una frazione (misurato: "
            "circa 52 righe su 801 eventi). Gli orari seguono inoltre il fuso "
            "impostato sul sito e i titoli la lingua dell'interfaccia. Usa il file "
            "dell'estrattore o la pagina salvata con lo stato JavaScript."
        )
    approssimati = int(meta.get("orario_approssimato") or 0)
    if approssimati:
        avvisi.append(
            f"{approssimati} eventi sono marcati 'All Day' o 'Tentative': la fonte "
            "stessa non ne conosce l'orario esatto. Restano in archivio per il "
            "giorno, ma non vanno letti come appuntamenti al minuto."
        )
    scartati = meta.get("scartati_dall_estrattore")
    if scartati:
        avvisi.append(
            f"L'estrattore ha scartato {scartati} righe senza timestamp: sono voci "
            "che il sito non data, non dati persi per un errore di lettura."
        )
    if meta.get("fuso") and str(meta["fuso"]).upper() not in ("UTC", "GMT"):
        avvisi.append(
            f"L'estrazione dichiara fuso '{meta['fuso']}': verifica che gli orari "
            "in dashboard coincidano con quelli che vedi sul sito."
        )
    return avvisi
