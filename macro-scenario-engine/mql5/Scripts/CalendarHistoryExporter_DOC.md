CalendarHistoryExporter — documentazione
Script MQL5 che esporta lo storico completo del calendario economico del terminale MetaTrader 5 in CSV, JSON e JSONL, con report di copertura.

Fonte dati: database calendario del terminale MT5, letto tramite l'API pubblica documentata MQL5 (CalendarValueHistory, CalendarEventById, CalendarCountryById). Nessuna richiesta HTTP, nessuno scraping, nessun accesso fuori dall'interfaccia fornita.


Installazione
Copia CalendarHistoryExporter.mq5 in <cartella dati MT5>\MQL5\Scripts\ (in MT5: File → Apri la cartella dei dati)
In MetaEditor premi F7 per compilare.
In MT5, dal Navigatore, trascina lo script su un grafico qualsiasi. Il grafico serve solo ad avviarlo: il calendario non dipende dal simbolo.

I file di output finiscono in MQL5\Files\ (oppure Common\Files\ se attivi InpUseCommonFolder).


Parametri
Intervallo
Parametro
Default
Significato
InpAutoDiscoverStart
true
Misura la prima data realmente presente nel calendario del tuo broker con una sonda anno per anno, poi mese per mese. Non assume il 2006.
InpDateFrom
2006.01.01
Usato solo se la discovery è disattivata.
InpDateTo
0
0 = adesso.
Filtri
Parametro
Default
Significato
InpCountryCode
""
Codice ISO 3166-1 alpha-2 (US, DE, …). Vuoto = tutti.
InpCurrency
""
Valuta (USD, EUR, …). Vuoto = tutte.
InpOnlyWithActual
false
Se true scarta gli eventi senza valore Actual.

Country e currency insieme si combinano in AND.
Acquisizione
Parametro
Default
Significato
InpChunkMonths
3
Ampiezza iniziale del blocco.
InpMaxRetries
4
Tentativi su un blocco già indivisibile.
InpPauseMs
20
Pausa fra blocchi.

Se un blocco restituisce ERR_CALENDAR_MORE_DATA (5400) o ERR_CALENDAR_TIMEOUT (5401), lo script lo dimezza ricorsivamente fino a 3 giorni. Solo se anche il blocco minimo fallisce dopo i retry, quel periodo viene marcato failed_explained nel report, col motivo. Non viene mai spacciato per vuoto.
Fuso orario
Parametro
Default
Significato
InpServerGmtOffset
999
Offset GMT del server di trading, in ore. 999 = non dichiarato.

Punto importante. L'API MQL5 restituisce gli orari nel fuso del server di trading, non in UTC e non nel tuo fuso locale. Convertire correttamente uno storico di 15 anni richiederebbe l'offset del server valido a ogni istante storico, che il terminale non espone — l'ora legale lo sposta due volte l'anno.

Di conseguenza:

time_server è sempre valorizzato, così com'è restituito dal terminale;
time_utc resta null finché non dichiari InpServerGmtOffset;
l'offset osservato al momento dell'export viene comunque registrato nel report, come riferimento, con nota esplicita che non si applica retroattivamente.

Se il tuo broker usa un offset fisso e lo conosci, impostalo e otterrai time_utc. Altrimenti il campo resta nullo: preferibile a una conversione sbagliata di un'ora su metà del dataset.


File prodotti
Prefisso: <InpPrefix>_<primadata>_<ultimadata>, per esempio mt5_calendar_20070102_20260815.

File
Contenuto
*.csv
UTF-8 con BOM (Excel lo apre correttamente), 29 colonne, ordinato cronologicamente.
*.json
Array JSON valido, stessi record, stesso ordine.
*.jsonl
Un oggetto JSON per riga, per elaborazione in streaming.
*_report.json
Metadati, copertura, conteggi, stato di ogni blocco.
Schema del record
value_id            id univoco del valore (chiave di deduplica)

event_id            id della serie: lo stesso indicatore ha sempre lo stesso event_id

time_server         ora del server di trading, ISO 8601

time_utc            null se l'offset non è dichiarato

period              periodo di riferimento del dato

revision            numero di revisione dell'indicatore

country_code        ISO 3166-1 alpha-2

country_name

currency

event_name

event_code

event_type          event | indicator | holiday

sector              gdp | jobs | prices | money | trade | ... | none

importance          none | low | moderate | high

frequency           day | week | month | quarter | year | none

time_mode           datetime | all_day | no_time | tentative

unit                percent | currency | job | barrel | ...

multiplier          none | thousands | millions | billions | trillions

digits              cifre decimali dichiarate per l'evento

actual              valore già diviso per 1.000.000 e arrotondato a `digits`

forecast

previous

revised_previous

actual_raw          intero originale del terminale (×10⁶), senza perdita

forecast_raw

previous_raw

revised_previous_raw

impact_type         na | positive | negative

source_url

Perché sia il valore formattato sia quello grezzo. MT5 memorizza i valori come interi moltiplicati per 10⁶. I campi *_raw conservano l'intero esatto; i campi formattati sono comodi ma passano da un arrotondamento. Se ti serve precisione piena, usa i *_raw.

Valori assenti. Nel terminale un valore mancante è LONG_MIN. Nell'export diventa null in JSON e cella vuota in CSV. Mai zero, mai una stringa inventata.

importance non è l'impatto di Forex Factory. È la tassonomia MetaQuotes a quattro livelli (none/low/moderate/high). Non mapparla su altre scale senza verificarla.


Report di copertura
*_report.json contiene:

coverage.records, raw_collected, duplicates_removed
coverage.first_record / last_record — misurati, non richiesti
coverage.with_actual, with_forecast, all_day, tentative
coverage.event_lookup_failures — eventi il cui metadato non è risolvibile
coverage.discovery_probe_errors — sonde di discovery fallite per errore, non per vuoto
coverage.chunks_completed / chunks_empty_verified / chunks_failed
by_year, by_currency, by_importance — conteggi reali
chunks[] — ogni blocco con from, to, values, state, reason

state vale completed, empty_verified o failed_explained. Un periodo senza dati è distinto da un periodo non acquisito.

L'esito finale nel log è COMPLETATO solo se chunks_failed == 0; altrimenti PARZIALE, e i blocchi mancanti sono elencati col motivo.


Limiti noti
La profondità storica dipende dal broker. Il calendario MT5 è sincronizzato dal server: broker diversi espongono profondità diverse. Per questo la discovery misura la prima data invece di assumerla. Esegui e leggi coverage.first_record.
Nello Strategy Tester il calendario è limitato. Esegui lo script sul terminale live, non in tester.
time_utc è null di default. Vedi la sezione Fuso orario.
Non c'è resume su disco. Lo script è monolitico: se lo interrompi, riparte da capo. Su un backfill completo servono tipicamente pochi minuti, non ore — se nel tuo caso non fosse così, spezza l'intervallo con InpDateFrom/InpDateTo ed esporta per blocchi di anni, poi unisci i JSONL con cat.
revised_previous è disponibile solo dove il terminale lo espone. Non è una ricostruzione delle revisioni storiche: per il point-in-time vero servono le vintage di ALFRED.


Aggiornamento incrementale
Per aggiungere solo il nuovo:

InpAutoDiscoverStart = false

InpDateFrom          = <ultima data già esportata>

InpDateTo            = 0

Il file avrà un prefisso diverso, e i due .jsonl si concatenano direttamente. La deduplica a valle si fa su value_id, che è stabile.


Verifica consigliata al primo utilizzo
Esegui con InpDateFrom = <un mese recente> e InpAutoDiscoverStart = false.
Apri il CSV e confronta cinque righe ad alto impatto con il calendario mostrato dal terminale (Vista → Strumenti → Calendario).
Controlla che coverage.chunks_failed sia 0.
Solo dopo lancia il backfill completo con la discovery attiva.

