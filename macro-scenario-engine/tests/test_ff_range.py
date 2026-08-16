"""Test dell'import storico da pagina Forex Factory con `range=`.

I campioni riproducono le tre forme che il parser deve reggere: l'estrazione
JSON dello snippet, lo stato JavaScript incorporato nella pagina salvata e la
tabella renderizzata. Sono campioni scritti a mano: servono a verificare la
logica del parser, non a certificare che il sito di oggi abbia questa forma —
quello nessun test locale puo' garantirlo, e infatti il parser dichiara il
fallimento invece di indovinare.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.collectors import ff_range
from app.storage.models import Event

# --------------------------------------------------------------------------- #
#  Campioni
# --------------------------------------------------------------------------- #

ESTRAZIONE = json.dumps(
    {
        "schema": "mse-ff-export/1",
        "url": "https://www.forexfactory.com/calendar?range=aug1.2019-oct2.2019",
        "range": "aug1.2019-oct2.2019",
        "metodo": "stato",
        "fuso": "UTC",
        "eventi": [
            {
                # 2019-08-02 12:30 UTC
                "name": "Non-Farm Employment Change",
                "currency": "USD",
                "dateline": 1564749000,
                "impactClass": "icon--ff-impact-red",
                "actual": "164K",
                "forecast": "165K",
                "previous": "193K",
                "revision": "193K",
                "timeLabel": "8:30am",
            },
            {
                # 2019-09-12 11:45 UTC — decisione BCE
                "name": "Main Refinancing Rate",
                "currency": "EUR",
                "dateline": 1568288700,
                "impactClass": "icon--ff-impact-red",
                "actual": "0.00%",
                "forecast": "0.00%",
                "previous": "0.00%",
                "timeLabel": "7:45am",
            },
            {
                "name": "Bank Holiday",
                "currency": "GBP",
                "dateline": 1567123200,
                "impactClass": "icon--ff-impact-gra",
                "actual": "",
                "forecast": "",
                "previous": "",
            },
        ],
    }
)

# Schema prodotto dall'estrattore verificato sul sito reale: i campi hanno i
# nomi che Forex Factory usa davvero (`ts`, `title`, `impact: high|medium|low`),
# diversi da quelli ipotizzati alla prima stesura. L'import deve reggere questo
# formato per primo: e' quello che arriva davvero dal browser.
ESTRAZIONE_REALE = json.dumps(
    {
        "schema": "ff-calendar/1",
        "source": "forexfactory.com/calendar",
        "sourceUrl": "https://www.forexfactory.com/calendar?range=aug1.2019-oct2.2019",
        "extractedAt": "2026-08-15T22:10:00.000Z",
        "method": "dom-state",
        "days": 63,
        "count": 4,
        "skipped": 2,
        "range": {"from": "2019-08-02", "to": "2019-09-12"},
        "events": [
            {
                "id": 112233,
                "ebaseId": 99,
                "ts": 1564749000,
                "utc": "2019-08-02T12:30:00.000Z",
                "currency": "USD",
                "country": "US",
                "title": "Non-Farm Employment Change",
                "impact": "high",
                "timeLabel": "8:30am",
                "timeApprox": False,
                "actual": "164K",
                "forecast": "165K",
                "previous": "193K",
                "revision": "193K",
                "actualBetterWorse": -1,
                "revisionBetterWorse": 0,
            },
            {
                "id": 112299,
                "ebaseId": 77,
                "ts": 1568288700,
                "utc": "2019-09-12T11:45:00.000Z",
                "currency": "EUR",
                "country": "EMU",
                "title": "Main Refinancing Rate",
                "impact": "high",
                "timeLabel": "7:45am",
                "timeApprox": False,
                "actual": "0.00%",
                "forecast": "0.00%",
                "previous": "0.00%",
                "revision": None,
            },
            {
                "id": 112300,
                "ebaseId": 55,
                "ts": 1565222400,
                "utc": "2019-08-08T00:00:00.000Z",
                "currency": "GBP",
                "country": "GB",
                "title": "MPC Member Speech",
                "impact": "medium",
                "timeLabel": "Tentative",
                "timeApprox": True,
                "actual": None,
                "forecast": None,
                "previous": None,
            },
            {
                "id": 112301,
                "ebaseId": 44,
                "ts": 1565308800,
                "utc": "2019-08-09T00:00:00.000Z",
                "currency": "JPY",
                "country": "JP",
                "title": "Bank Holiday",
                "impact": "holiday",
                "timeLabel": "All Day",
                "timeApprox": True,
                "actual": None,
                "forecast": None,
                "previous": None,
            },
        ],
    }
)

STATO_HTML = """
<!doctype html><html><head><title>Calendar</title></head><body>
<script>
window.calendarComponentStates = window.calendarComponentStates || {};
calendarComponentStates[1] = {"days": [
  {"date": "Thu Aug 1", "events": [
     {"id": 1, "name": "ISM Manufacturing PMI", "currency": "USD",
      "dateline": 1564660800, "impactClass": "icon--ff-impact-red",
      "actual": "51.2", "forecast": "52.0", "previous": "51.7"}
  ]},
  {"date": "Fri Aug 2", "events": [
     {"id": 2, "name": "Average Hourly Earnings m/m", "currency": "USD",
      "dateline": 1564749000, "impactClass": "icon--ff-impact-ora",
      "actual": "0.3%", "forecast": "0.2%", "previous": "0.3%"}
  ]}
]};
</script>
</body></html>
"""

TABELLA_HTML = """
<html><body><table><tbody>
<tr class="calendar__row">
  <td class="calendar__date">Thu<span>Aug 1</span></td>
  <td class="calendar__time">10:00am</td>
  <td class="calendar__currency">USD</td>
  <td class="calendar__event">ISM Manufacturing PMI</td>
  <td class="calendar__impact"><span class="icon--ff-impact-red"></span></td>
  <td class="calendar__actual">51.2</td>
  <td class="calendar__forecast">52.0</td>
  <td class="calendar__previous">51.7</td>
</tr>
<tr class="calendar__row">
  <td class="calendar__date"></td>
  <td class="calendar__time">2:00pm</td>
  <td class="calendar__currency">USD</td>
  <td class="calendar__event">Construction Spending m/m</td>
  <td class="calendar__impact"><span class="icon--ff-impact-yel"></span></td>
  <td class="calendar__actual">-1.3%</td>
  <td class="calendar__forecast">0.3%</td>
  <td class="calendar__previous">-0.5%</td>
</tr>
</tbody></table></body></html>
"""


# --------------------------------------------------------------------------- #
#  Estrazione dello snippet
# --------------------------------------------------------------------------- #


def test_estrazione_legge_timestamp_unix():
    """Il timestamp UNIX e' assoluto: l'orario non dipende dal fuso del sito."""
    rows, meta = ff_range.parse_extraction(ESTRAZIONE)

    assert meta["metodo"] == "stato"
    assert len(rows) == 3

    nfp = next(r for r in rows if "Non-Farm" in r["title"])
    assert nfp["timestamp_utc"] == datetime(2019, 8, 2, 12, 30)
    assert nfp["currency"] == "USD"
    assert nfp["impact"] == "RED"
    assert nfp["actual"] == pytest.approx(164.0)
    assert nfp["forecast"] == pytest.approx(165.0)
    assert nfp["unit"] == "K"


def test_estrazione_classifica_decisione_tassi():
    """Le decisioni di tasso devono essere riconosciute: servono alla rigiocata."""
    rows, _ = ff_range.parse_extraction(ESTRAZIONE)
    bce = next(r for r in rows if r["currency"] == "EUR")
    assert bce["category"] == "RATE_DECISION"
    assert bce["actual"] == pytest.approx(0.0)


def test_estrazione_senza_valori_resta_importabile():
    """Una festivita' non ha valori: si importa lo stesso, senza inventare zeri."""
    rows, _ = ff_range.parse_extraction(ESTRAZIONE)
    festa = next(r for r in rows if r["currency"] == "GBP")
    assert festa["actual"] is None
    assert festa["forecast"] is None


def test_estrazione_non_valida_dichiara_il_problema():
    with pytest.raises(ff_range.RangeParseError):
        ff_range.parse_extraction('{"qualcosa": 1}')


# --------------------------------------------------------------------------- #
#  Schema reale dell'estrattore (campi verificati sul sito)
# --------------------------------------------------------------------------- #


def test_schema_reale_viene_letto():
    """`ts` invece di `dateline`, `events` invece di `eventi`, `impact: high`."""
    rows, meta = ff_range.parse_extraction(ESTRAZIONE_REALE)

    assert meta["schema"] == "ff-calendar/1"
    assert meta["metodo"] == "stato"  # 'dom-state' normalizzato
    assert len(rows) == 4

    nfp = next(r for r in rows if "Non-Farm" in r["title"])
    assert nfp["timestamp_utc"] == datetime(2019, 8, 2, 12, 30)
    assert nfp["impact"] == "RED"  # 'high'
    assert nfp["actual"] == pytest.approx(164.0)
    assert nfp["revised"] == pytest.approx(193.0)


def test_schema_reale_mappa_i_livelli_di_impatto():
    rows, _ = ff_range.parse_extraction(ESTRAZIONE_REALE)
    per_valuta = {r["currency"]: r["impact"] for r in rows}
    assert per_valuta["USD"] == "RED"       # high
    assert per_valuta["GBP"] == "ORANGE"    # medium
    assert per_valuta["JPY"] == "YELLOW"    # holiday


def test_schema_reale_riconosce_la_decisione_tassi():
    """Serve alla rigiocata: senza questa i tassi storici non si ricostruiscono."""
    rows, _ = ff_range.parse_extraction(ESTRAZIONE_REALE)
    bce = next(r for r in rows if r["currency"] == "EUR")
    assert bce["category"] == "RATE_DECISION"


def test_schema_reale_segnala_gli_orari_non_affidabili(session):
    """'All Day' e 'Tentative' non sono appuntamenti al minuto: va detto."""
    report = ff_range.import_range(session, ESTRAZIONE_REALE)
    assert report["eventi"] == 4
    assert any("All Day" in a or "Tentative" in a for a in report["avvisi"])
    assert any("scartat" in a for a in report["avvisi"])  # skipped: 2


def test_lista_nuda_di_eventi_accettata():
    """Un file ridotto alla sola lista resta importabile."""
    solo_eventi = json.dumps(json.loads(ESTRAZIONE_REALE)["events"])
    rows, meta = ff_range.parse_extraction(solo_eventi)
    assert len(rows) == 4
    assert meta["metodo"] == "stato"


# --------------------------------------------------------------------------- #
#  Stato JavaScript nella pagina salvata
# --------------------------------------------------------------------------- #


def test_stato_javascript_viene_estratto():
    rows, meta = ff_range.parse_state_html(STATO_HTML)
    assert meta["metodo"] == "stato"
    assert len(rows) == 2
    pmi = next(r for r in rows if "ISM" in r["title"])
    assert pmi["timestamp_utc"] == datetime(2019, 8, 1, 12, 0)
    assert pmi["impact"] == "RED"


def test_stato_assente_solleva_errore_leggibile():
    with pytest.raises(ff_range.RangeParseError) as exc:
        ff_range.parse_state_html("<html><body>niente</body></html>")
    assert "calendarComponentStates" in str(exc.value)


def test_parentesi_bilanciate_ignorano_le_stringhe():
    """Una graffa dentro una stringa non deve chiudere l'oggetto in anticipo."""
    testo = 'calendarComponentStates[0] = {"a": "} finto", "b": {"c": 1}}; resto'
    blocchi = ff_range.extract_state_blocks(testo)
    assert blocchi == [{"a": "} finto", "b": {"c": 1}}]


# --------------------------------------------------------------------------- #
#  Tabella renderizzata
# --------------------------------------------------------------------------- #


def test_tabella_trascina_la_data_sulle_righe_successive():
    """La data e' valorizzata solo sulla prima riga del giorno."""
    rows, meta = ff_range.parse_table_html(TABELLA_HTML, year=2019)
    assert meta["metodo"] == "tabella"
    assert len(rows) == 2
    assert all(r["timestamp_utc"].date() == datetime(2019, 8, 1).date() for r in rows)
    assert rows[0]["timestamp_utc"].hour == 10
    assert rows[1]["timestamp_utc"].hour == 14  # 2:00pm


def test_tabella_senza_anno_usa_il_range_dell_indirizzo():
    """Senza l'anno lo storico finirebbe silenziosamente nell'anno corrente."""
    html = (
        '<html><head><link href="/calendar?range=aug1.2019-oct2.2019"></head>'
        + TABELLA_HTML
    )
    rows, _ = ff_range.parse_table_html(html)
    assert rows[0]["timestamp_utc"].year == 2019


def test_range_param_riconosciuto():
    periodo = ff_range.parse_range_param("?range=aug1.2019-oct2.2019")
    assert periodo == (datetime(2019, 8, 1), datetime(2019, 10, 2))
    assert ff_range.parse_range_param("?range=rubbish") is None


# --------------------------------------------------------------------------- #
#  Riconoscimento automatico e persistenza
# --------------------------------------------------------------------------- #


def test_parse_any_riconosce_json_e_stato():
    assert ff_range.parse_any(ESTRAZIONE)[1]["metodo"] == "stato"
    assert ff_range.parse_any(ESTRAZIONE_REALE)[1]["metodo"] == "stato"
    assert ff_range.parse_any(STATO_HTML)[1]["metodo"] == "stato"


def test_la_tabella_non_parte_mai_da_sola():
    """Misurato sulla pagina reale: nel file salvato c'e' circa il 7% delle righe.

    Importarne una frazione dichiarando successo e' peggio di un errore: l'errore
    si vede, una sigma stimata sul 7% del campione no.
    """
    with pytest.raises(ff_range.RangeParseError) as exc:
        ff_range.parse_any(TABELLA_HTML, year=2019)
    assert "tabella HTML non viene usata come ripiego" in str(exc.value)


def test_la_tabella_si_puo_chiedere_esplicitamente():
    rows, meta = ff_range.parse_any(TABELLA_HTML, year=2019, consenti_tabella=True)
    assert meta["metodo"] == "tabella"
    assert len(rows) == 2


def test_import_da_tabella_dichiara_che_e_parziale(session):
    report = ff_range.import_range(session, TABELLA_HTML, year=2019, consenti_tabella=True)
    assert report["metodo"] == "tabella"
    assert any("INCOMPLETO" in a for a in report["avvisi"])


def test_mhtml_riconosciuto_con_istruzione_utile():
    """Il salvataggio "Singolo file" di Chrome non contiene lo stato."""
    mhtml = (
        "From: <Saved by Blink>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/related; boundary="----=_NextPart_000"\r\n\r\n'
        "------=_NextPart_000\r\n"
    )
    with pytest.raises(ff_range.RangeParseError) as exc:
        ff_range.parse_any(mhtml)
    messaggio = str(exc.value)
    assert "MHTML" in messaggio and "Singolo file" in messaggio


def test_stato_grezzo_con_days_in_cima_accettato():
    """Lo stato del sito salvato cosi' com'e', senza involucro."""
    grezzo = json.dumps(
        {
            "days": [
                {
                    "date": "Thu Aug 1",
                    "events": [
                        {
                            "id": 1,
                            "name": "ISM Manufacturing PMI",
                            "currency": "USD",
                            "dateline": 1564660800,
                            "impactName": "high",
                            "actual": "51.2",
                            "forecast": "52.0",
                        }
                    ],
                }
            ]
        }
    )
    rows, meta = ff_range.parse_any(grezzo)
    assert len(rows) == 1
    assert rows[0]["impact"] == "RED"
    assert meta["metodo"] == "stato"


def test_file_unito_riconosciuto():
    """Il file prodotto unendo piu' range: method 'merged'."""
    unito = json.dumps(
        {
            "schema": "ff-calendar/1",
            "method": "merged",
            "events": json.loads(ESTRAZIONE_REALE)["events"],
        }
    )
    rows, meta = ff_range.parse_any(unito)
    assert meta["metodo"] == "unione"
    assert len(rows) == 4


def test_parse_any_su_contenuto_ignoto_dichiara_il_motivo():
    with pytest.raises(ff_range.RangeParseError) as exc:
        ff_range.parse_any("<html><body><p>pagina sbagliata</p></body></html>")
    messaggio = str(exc.value)
    assert "non sembra" in messaggio.lower()
    assert "stato JavaScript" in messaggio


def test_import_salva_e_calcola_le_sorprese(session):
    report = ff_range.import_range(session, ESTRAZIONE)
    session.flush()

    assert report["eventi"] == 3
    assert report["creati"] == 3
    assert report["metodo"] == "stato"

    salvati = session.query(Event).all()
    assert len(salvati) == 3

    nfp = next(e for e in salvati if "Non-Farm" in e.title)
    assert nfp.timestamp_utc == datetime(2019, 8, 2, 12, 30)
    assert nfp.source == ff_range.SOURCE

    # Gli avvisi devono dire quanti eventi hanno davvero il valore effettivo.
    assert any("valore effettivo" in a for a in report["avvisi"])


def test_import_ripetuto_aggiorna_non_duplica(session):
    ff_range.import_range(session, ESTRAZIONE)
    session.flush()
    secondo = ff_range.import_range(session, ESTRAZIONE)
    session.flush()

    assert secondo["creati"] == 0
    assert secondo["aggiornati"] == 3
    assert session.query(Event).count() == 3


def test_import_html_senza_stato_non_ripiega_sulla_tabella(session):
    """Un HTML senza stato deve fallire, non importare una frazione degli eventi."""
    with pytest.raises(ff_range.RangeParseError):
        ff_range.import_range(session, TABELLA_HTML, year=2019)
    assert session.query(Event).count() == 0
