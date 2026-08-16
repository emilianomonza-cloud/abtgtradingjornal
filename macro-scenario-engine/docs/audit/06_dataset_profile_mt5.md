# FASE 3 — Profilo del dataset storico MT5 (dati reali)

Fonte: `mt5_calendar_20070106_20260731_report.json` prodotto dall'exporter sul
terminale dell'utente (broker Onam Trading, build 6090) e file dati su Drive:
CSV 52,4 MB · JSON 139,7 MB · JSONL 139,5 MB.

## Copertura misurata (non stimata)

| Metrica | Valore |
|---|---|
| Record totali | **209.309** |
| Periodo | 2007-01-06 → 2026-07-31 (19,6 anni) |
| Con valore effettivo | 193.397 (92,4%) |
| Con consenso | 74.645 (35,7%) — la sorpresa è calcolabile solo qui |
| All Day / Tentative | 3.091 + 12.786 (7,6% con orario non affidabile) |
| Blocchi acquisiti | 79/79 completati, **0 falliti**, 0 lookup falliti |
| Duplicati rimossi dall'exporter | 0 |

Per anno: crescita monotona da 1.852 (2007) a ~14.900 (2020-2023) — la
profondità utile per stime di σ robuste parte all'incirca dal 2012 (8.036/anno).

Per valuta (19): USD 52.517, EUR 44.914, JPY 18.536, GBP 16.964, AUD 9.442,
CAD 9.264, NZD 6.941, CHF 4.680 — tutte le valute configurate nel motore sono
ben coperte. Presenti anche BRL/MXN/INR/ZAR/CNY/KRW/SEK/NOK/SGD/HKD/ALL:
importate ma inerti finché non configurate in `operator.yaml`.

Per importanza: high 15.070 · moderate 72.498 · low 119.577 · none 2.164 →
con la mappa dichiarata: RED 15.070, ORANGE 72.498, YELLOW 119.577, GRAY 2.164.
La quota RED (7,2%) è plausibile per un calendario completo: la mappa regge.

## Il problema rilevato dal report: fuso orario

`declared_gmt_offset_hours: null` → **`time_utc` è null su tutte le righe**;
gli orari sono nell'ora del server del broker. Offset osservato all'export:
**−9,01 h** rispetto a GMT (misurato al momento dell'export; non vale
retroattivamente attraverso i cambi di ora legale).

Mitigazione implementata: l'import accetta `gmt_offset_hours` (campo "Offset
server" nella pagina /storico, query param sull'API). Con `-9` gli orari
diventano UTC a meno dell'errore di ora legale (±1h su parte dello storico),
dichiarato nel report di import. In alternativa: ri-esportare con
`InpServerGmtOffset` impostato. **L'errore residuo è di ore, mai di giorno**:
sposta le finestre no-trade intraday, non le sorprese né gli score giornalieri.

## Conseguenze per il motore

- 74.645 eventi con consenso ⇒ decine di migliaia di sorprese calcolabili, su
  19,6 anni: base sufficiente per σ per-indicatore e per la rigiocata lunga.
- Le decisioni di tasso presenti per tutte le valute G8 ⇒ ricostruzione dei
  tassi storici per la rigiocata su tutto il periodo.
- Il profilo di copertura è ora calcolato anche AL MOMENTO DELL'IMPORT
  (`copertura` nel report dell'endpoint: per anno, valuta, impatto, categoria,
  actual/forecast/revisioni) e mostrato nella pagina /storico: il confronto
  con il `*_report.json` dell'exporter chiude il cerchio della FASE 3.

## Prestazioni misurate (benchmark a 209k righe, distribuzioni reali)

| Percorso | Esito |
|---|---|
| Normale (pre-fix) | **>10 minuti, interrotto**: ogni autoflush riscandagliava un'identity map da centinaia di migliaia di oggetti |
| A lotti (attuale) | **279,5 s** per 209.309 eventi e 191.876 sorprese (749 eventi/s) |
| Re-import dello stesso file | **232,8 s**: aggiorna tutte le righe senza duplicare (dedup per contenuto) |

L'import completo dei 19,6 anni e' quindi un'operazione da ~5 minuti, una
tantum. La pagina /storico lo dichiara prima che l'utente prema il pulsante.

## Limite di questo ambiente (dichiarato)

Il file completo non è scaricabile in questa sandbox (proxy 403 su Drive
download; la via MCP restituirebbe ~70 MB base64 inline). Il profiling del
file intero gira quindi sul PC dell'utente, dentro l'import — by design: i
dati stanno dove sta il server. In sandbox la validazione è fatta su fixture
che replicano lo schema del produttore versionato e su un benchmark sintetico
a 209k righe con le distribuzioni del report reale.
