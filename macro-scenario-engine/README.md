# Macro Scenario Engine

Analisi fondamentale FX con **scenari probabilistici**: raccoglie il calendario
economico e i comunicati delle banche centrali, applica un modello macro causale
esplicito, e produce per ogni coppia tre scenari con probabilità, confidenza,
condizioni di invalidazione e scadenza. Gli scenari arrivano su MetaTrader 5
tramite un indicatore e un Expert Advisor di filtro.

> **Le probabilità sono stime di modello, non garanzie.** Lo scenario base non
> può superare il 65%, ogni scenario dichiara le sue condizioni di invalidazione
> e la dashboard mostra sempre hit-rate e Brier score storici — anche quando
> sono impietosi. Non è consulenza finanziaria.

---

## Avvio più semplice: doppio clic

| Sistema | File |
|---|---|
| Windows | **`avvia_windows.bat`** |
| macOS / Linux | **`avvia_mac_linux.sh`** (`chmod +x` la prima volta) |

Lo script crea l'ambiente virtuale, installa le dipendenze, prepara il `.env`,
avvia il server e apre il browser. La prima volta impiega qualche minuto.

> ⚠️ Questi sono script di sistema, **non codice Python**: non vanno incollati
> nell'interprete Python (quello con il prompt `>>>`).

Se la finestra nera si apre e si chiude subito, aprila da terminale per leggere
il messaggio: tasto destro sulla cartella → *Apri nel Terminale*, poi scrivi
`avvia_windows.bat` e premi Invio.

Vuoi vedere la dashboard già popolata? **`carica_dati_demo_windows.bat`**
(oppure `python carica_dati_demo.py` da `backend/`) inserisce eventi, comunicati
e uno storico di scenari **dimostrativi e inventati**, utili solo a capire come
si comporta il sistema.

## Avvio manuale in 5 comandi

Da **Prompt dei comandi / Terminale** (non da Python):

```bash
cd macro-scenario-engine/backend
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                    # Windows: copy .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Poi apri **http://127.0.0.1:8000** (dashboard) e **http://127.0.0.1:8000/docs**
(API). Al primo avvio il database SQLite viene creato in `data/`, i tassi di
policy di bootstrap vengono inseriti e **i collector partono entro ~75 secondi**
(calendario a +5s, banche centrali a +30s, primo calcolo scenari a +75s).

Con Docker:

```bash
cd macro-scenario-engine
cp backend/.env.example backend/.env
docker compose up --build
```

**Nessuna chiave API è necessaria.** Il layer semantico funziona in modalità
rule-based; l'LLM è un potenziamento opzionale (vedi §7).

**Nessuna compilazione è necessaria.** Tutte le dipendenze si installano come
pacchetti precompilati: niente Visual Studio, niente Rust. `lxml` e `anthropic`
sono opzionali e restano fuori dall'installazione standard proprio perché su
alcune versioni di Python richiederebbero un compilatore.

**Versione di Python:** consigliata **3.12 o 3.13**. Sulle versioni appena
uscite alcuni pacchetti possono non essere ancora disponibili precompilati; lo
script di avvio sceglie automaticamente la versione più adatta fra quelle
installate.

---

## Da dove arrivano i dati (e perché all'inizio è tutto NEUTRAL)

Se la dashboard mostra **NEUTRAL su ogni coppia**, quasi sempre non è il modello
che "non ha un'opinione": è il database vuoto. Senza eventi in archivio tutti e
sei i sotto-score valgono 0, quindi ogni bias vale 0, quindi lo scenario più
probabile è per forza il laterale. Il modello non inventa numeri quando non ha
informazioni — e ora te lo dice con un banner rosso in Overview.

**Le tre strade per avere dati veri:**

1. **Automatica.** I collector girano da soli: calendario ogni ora, banche
   centrali ogni 3 ore, e — dalla versione corrente — **subito dopo l'avvio**.
   Prima partivano solo alla scadenza del primo intervallo: chi apriva la
   dashboard nei primi 60 minuti vedeva un archivio vuoto senza capire perché.
2. **Manuale, immediata.** Il pulsante **"Scarica i dati adesso"** in Overview e
   in `/fonti`, oppure `POST /api/v1/admin/collect` da `/docs`. Serve quando vuoi
   forzare un aggiornamento senza aspettare il ciclo.
3. **Import CSV**, se una fonte è bloccata dalla tua rete: `POST
   /api/v1/admin/calendar/import` accetta eventi in CSV o JSON. È il fallback
   dichiarato: nessuna fonte è un punto singolo di rottura.

**Cosa scarica, esattamente:** il feed JSON settimanale di Forex Factory
(`nfs.faireconomy.media`) per il calendario, e i feed RSS ufficiali di FED, BCE,
BoE, BoJ, RBA, BoC, SNB e RBNZ per comunicati e discorsi. Tutto pubblico, tutto
senza chiave API. Se una fonte risponde male, la pagina `/fonti` la marca
DEGRADED o DOWN, il sistema prosegue con le altre e **abbassa la confidenza**
degli scenari invece di fingere.

**Dopo il primo scaricamento** la griglia valute smette di essere piatta: le
sorprese sui dati usciti muovono il sotto-score `surprise`, i comunicati
alimentano `cb_stance`, i tassi di policy alimentano `rate_differential`. I
sotto-score `inflation_regime`, `growth_momentum` ed `external_balance` si
riempiono man mano che escono i rispettivi dati, perché richiedono uno storico
per stimare la σ delle sorprese (servono almeno 6 rilasci per indicatore).

**La pagina `/affidabilita` resta invece vuota per giorni, ed è corretto così.**
Hit-rate e Brier score misurano scenari *già scaduti* e confrontati con i prezzi
reali: richiedono (a) che l'orizzonte dello scenario sia concluso e (b) che tu
abbia importato i prezzi da MT5 (§8). Sotto i 30 scenari valutati compare
"campione insufficiente" perché con meno dati qualsiasi percentuale sarebbe
rumore spacciato per performance. Non è riempibile il primo giorno senza
inventare uno storico — e inventarlo sarebbe esattamente ciò che questo progetto
si vieta.

Per **vedere come appare la dashboard piena** senza aspettare, usa
`carica_dati_demo_windows.bat`: carica eventi, comunicati e 42 scenari già
valutati (hit-rate 52.4%, Brier 0.248). Sono **dati inventati a scopo
dimostrativo**, etichettati come tali: non usarli per decidere niente.

---

## Storico reale (pagina `/storico`)

### Fonte consigliata: il calendario del tuo terminale MT5

MetaTrader 5 contiene già anni di calendario economico con valori effettivi,
consensi e revisioni. Lo script **`mql5/Scripts/CalendarHistoryExporter.mq5`**
(documentato in `mql5/Scripts/CalendarHistoryExporter_DOC.md`) lo esporta in
CSV/JSON/JSONL con un report di copertura, leggendo solo il database locale
del terminale: nessuna richiesta web, nessun account.

**Installazione con doppio clic su tutti i terminali del PC:**
`installa_mql5_windows.bat` trova ogni installazione MT5 presente
(`%APPDATA%\MetaQuotes\Terminal\*`), copia exporter, indicatore, EA e include
in ciascuna e compila dove riesce a individuare MetaEditor (via `origin.txt`);
dove non riesce lo dice, e resta la F7. I terminali avviati in modalità
`/portable` non compaiono lì e vanno fatti a mano — limite dichiarato dallo
script stesso.

Poi, in ogni terminale:

1. Navigator → Scripts → **CalendarHistoryExporter** → trascina sul grafico.
2. I file dello storico compaiono in `MQL5\Files\` (File → Apri cartella dati).
3. Carica il `.csv`/`.json`/`.jsonl` nella pagina `/storico` (sezione 1).

L'import preferisce i campi `*_raw` (interi ×10⁶, precisione piena), usa
`time_utc` quando c'è e altrimenti l'ora del server **dichiarandolo**, e
applica una mappa di impatto esplicita (importance MetaQuotes high→RED,
moderate→ORANGE, low→YELLOW, none/holiday→GRAY) stampata in ogni report.

Se l'export non aveva l'offset dichiarato (nel `*_report.json`:
`declared_gmt_offset_hours: null`), puoi dichiararlo **all'import** nel campo
"Offset server" (il valore osservato è `observed_offset_at_export_hours`):
gli orari vengono convertiti in UTC, con l'avviso che attraverso i cambi di
ora legale la correzione a offset fisso può sbagliare di un'ora.

Sopra le 5.000 righe l'import passa a un percorso **a lotti** (ordinato per
timestamp: la σ di ogni evento vede solo il suo passato anche nel massivo) e
il report include il **profilo di copertura** per anno, valuta, impatto e
categoria — il confronto col `*_report.json` dell'exporter chiude la verifica.

### Alternativa: import da Forex Factory e rigiocata

Il feed pubblico copre solo la settimana corrente e l'export CSV del sito
richiede un account. Lo storico però è visibile a chiunque apra la pagina del
periodo, per esempio
`https://www.forexfactory.com/calendar?range=aug1.2019-oct2.2019`.

La strada implementata è **estrarlo dal browser**, non raschiarlo dal server:

1. Apri la pagina del periodo e scorri fino in fondo.
2. `F12` → Console (in Chrome, la prima volta, scrivi `allow pasting`).
3. Incolla `backend/app/web/static/js/estrai_forexfactory.js` — scaricabile anche
   da `/static/js/estrai_forexfactory.js` con il server acceso.
4. Parte il download di un `.json`. Caricalo nella pagina **`/storico`**.

Lo snippet non fa nessuna richiesta di rete: legge i dati che la pagina ha già
caricato per te. È il traffico di una persona che consulta una pagina, non di un
crawler — nessun rate limit da rispettare perché non c'è nessuna richiesta
aggiuntiva, e nessun login da aggirare.

Formati accettati dall'import, **entrambi con la stessa fedeltà**:

| Formato | Come si ottiene | Note |
|---|---|---|
| `.json` dello snippet | Console del browser | timestamp UNIX assoluti |
| `.html` salvato | `Ctrl`+`S` sulla pagina | legge lo `<script>` con lo stato: **stessi timestamp assoluti** |

Non serve impostare il fuso del sito su GMT prima di salvare: il fuso cambia le
etichette visibili, non i timestamp. In Chrome scegli però *"Pagina web,
completa"* o *"Solo HTML"* — *"Singolo file"* produce un archivio MHTML che non
contiene lo stato (l'import lo riconosce e lo dice).

### Unire più periodi prima di importare

`strumenti/storico.html` si apre con un doppio clic e funziona **a server
spento**: ci trascini dentro tutti i `.json` dei vari trimestri, li unisce
deduplicando sull'`id` evento di Forex Factory (stabile fra range diversi, quindi
i periodi sovrapposti non creano doppioni), ti lascia filtrare e ordinare, ed
esporta un `.json` unico o un CSV.

Serve soprattutto a **guardare i dati prima di fidarsene**: se un trimestre è
mezzo vuoto lo vedi lì, non dopo che ha sporcato le stime di σ.

### Perché la tabella HTML non viene letta

Misurato sulla pagina reale: la tabella è renderizzata in modo pigro e in una
pagina salvata sono presenti **circa 52 righe su 801 eventi**. Un parser della
tabella importerebbe il 7% dei dati dichiarando "importato con successo" — ed è
peggio di un errore, perché l'errore lo vedi e una σ stimata sul 7% del campione
no. Ci sono anche due difetti minori: i titoli seguono la lingua dell'interfaccia
(quindi non sono confrontabili fra file) e la riga del giorno non porta l'anno.

Il parser della tabella resta nel codice ma **non parte mai da solo**: va chiesto
con `consenti_tabella=true`, e in quel caso il report lo dichiara come import
parziale. Se nessun percorso riconosce il file, l'import restituisce **422 con il
motivo**, non zero eventi in silenzio.

L'estrattore legge `window.calendarComponentStates[N].days[].events[]` e ricopia
il campo `dateline`, che è un **timestamp UNIX assoluto**: gli orari non
dipendono dal fuso impostato sul profilo. Gli eventi marcati `All Day` o
`Tentative` vengono importati con l'orario che la fonte dichiara, ma l'import
segnala quanti sono: la fonte stessa non ne conosce il minuto esatto.

Il parser accetta sia lo schema `ff-calendar/1` (campi `events`, `ts`, `title`,
`impact: high|medium|low|holiday`) sia una lista nuda di eventi, così lo snippet
può essere aggiornato senza dover toccare il backend.

### Rigiocare il passato per riempire `/affidabilita`

Con lo storico del calendario **e** lo storico prezzi (esportato da MT5:
*Strumenti → Archivio quotazioni → Esporta*) la pagina `/storico` può generare
scenari alle date passate e valutarli subito, invece di aspettare settimane.

Il meccanismo: per ogni data della griglia l'orologio del sistema viene congelato
(`repositories.clock_frozen_at`) e la pipeline gira normalmente. Congelare
l'orologio invece di passare una "data di riferimento" a ogni funzione è la
garanzia che **nessun ramo del motore possa leggere dati successivi alla data
simulata** — compresi i filtri `now_utc()` in profondità. C'è un test dedicato a
questa proprietà.

I tassi di policy vengono **ricostruiti dalle decisioni presenti nel calendario
storico**: usare quelli di oggi per il 2019 sarebbe un dato del futuro. Dove il
calendario non copre una banca centrale, quella valuta resta senza tasso e il
sotto-score entra come neutro, con qualità zero.

**Cosa NON sono questi numeri** — dichiarato nel report, nell'API e in dashboard:

- **non** è un backtest di strategia: non ci sono spread, slippage o commissioni;
- il calendario riporta i valori **revisionati**, non la prima stampa: è una
  forma di lookahead non eliminabile da questa fonte;
- nel passato non esistono comunicati delle banche centrali in archivio, quindi
  il sotto-score `cb_stance` (15%) è quasi sempre neutro e gli scenari rigiocati
  sono più poveri di quelli generati in tempo reale;
- gli scenari rigiocati sono marcati `origine: rigiocata_storica` e restano
  distinguibili da quelli veri per sempre, anche nell'hit-rate.

Una rigiocata è un indizio sulla qualità del modello. Non è un rendimento.

---

## 1. Cosa fa, in concreto

```
 Forex Factory (feed JSON)  ─┐
 RSS banche centrali        ─┤→  collector  →  eventi + documenti normalizzati
 CSV manuale (fallback)     ─┘                        │
                                                      ▼
                                    catene causali esplicite (SEZIONE 3.1)
                                                      │
                     score valutario a 6 sotto-score  ▼  spiegabile riga per riga
                                                      │
                            bias di coppia  →  3 scenari probabilistici
                                                      │
                    ┌─────────────────────────────────┼──────────────────────┐
                    ▼                                 ▼                      ▼
              dashboard web                    API JSON locale        bridge MQL5
```

Il modello non è una black box: ogni score dichiara **quali catene causali** l'hanno
prodotto, con quali dati e da quale fonte.

---

## 2. Struttura del progetto

```
macro-scenario-engine/
├── backend/
│   ├── app/
│   │   ├── collectors/     calendario (Forex Factory + fallback), RSS banche centrali
│   │   ├── enrichment/     classificatore hawkish/dovish (rule-based + hook LLM)
│   │   ├── engine/         catene causali, sorprese, scoring, scenari, playbook
│   │   ├── calibration/    valutazione ex-post, hit-rate, Brier, rigiocata storica
│   │   ├── storage/        modelli ORM, SQLite, repository
│   │   ├── api/            endpoint pubblici + amministrativi
│   │   ├── scheduler/      job ricorrenti (APScheduler)
│   │   ├── web/            dashboard server-side (Jinja2)
│   │   ├── config/         *.yaml modificabili senza toccare il codice
│   │   ├── pipeline.py     score → scenari → snapshot MT5 (con cache)
│   │   └── main.py         applicazione FastAPI
│   ├── demo_nfp.py         FASE 9: simulazione NFP BIG_BEAT end-to-end
│   ├── carica_dati_demo.py dati dimostrativi per vedere la dashboard piena
│   ├── export_mt5_file.py  esportatore di macro_snapshot.json per MT5
│   └── requirements.txt
├── strumenti/              estrattore e visualizzatore offline (doppio clic, server spento)
├── mql5/
│   ├── Include/MacroScenarioTypes.mqh      strutture + parser JSON
│   ├── Indicators/MacroScenarioBridge.mq5  pannello + 4 buffer
│   ├── Experts/MacroScenarioEA.mq5         filtro rischio (DRY-RUN di default)
│   └── README_MQL5.md                      installazione e uso
├── data/                   database, cache, export (creata al primo avvio)
├── tests/                  156 test (catene, sorprese, scenari, import storico, rigiocata, API)
├── avvia_windows.bat       avvio automatico (doppio clic)
├── avvia_mac_linux.sh      avvio automatico su macOS/Linux
├── docker-compose.yml
└── README.md
```

---

## 3. Il modello macro (SEZIONE 3)

### Catene causali codificate

Ogni catena è un oggetto con i passi dichiarati in chiaro, consultabile su
`GET /api/v1/chains` e mostrata al click in dashboard:

| Catena | Ramo positivo → effetto sulla valuta |
|---|---|
| `TASSI_CAPITALI_CAMBIO` | i interno ↑ → attività nazionali più attraenti → afflussi di capitale ↑ → domanda valuta ↑ → **apprezzamento** |
| `MONETARIA_ESPANSIVA` | M ↑ → i ↓ → investimenti ↑ → PIL ↑, ma afflussi ↓ → **deprezzamento** → export ↑ → PIL ↑ |
| `MONETARIA_RESTRITTIVA` | inflazione alta → i ↑ → afflussi ↑ → **apprezzamento**, investimenti ↓ → PIL rallenta |
| `PIL_CRESCITA` | PIL ↑ → occupazione ↑ → redditi ↑ → consumi ↑ → (via attese di policy) **valuta forte** |
| `INFLAZIONE_COMPETITIVITA` | prezzi interni ↑ → cambio reale ↑ → competitività ↓, ma in regime hawkish domina il canale capitali → **valuta ↑** |
| `INFLAZIONE_CREDIBILITA` | prezzi ↑ con banca centrale bloccata → canale competitività dominante → **valuta ↓** |
| `BILANCIA_PAGAMENTI` | BP > 0 → domanda valuta ↑ → **apprezzamento** |
| `COMMERCIO_ESTERO` | esportazioni ↑ → domanda valuta ↑ → **apprezzamento** |

Il **regime** (`HAWKISH_DOMINANT` o `CREDIBILITY_LOSS`) decide quale canale
domina sull'inflazione, è configurabile per valuta e viene **dichiarato in ogni
valutazione**.

### Score valutario

| Sotto-score | Peso | Input |
|---|---:|---|
| Differenziale tassi | 25% | tassi policy vs media pari, sorprese di policy |
| Regime inflazione | 20% | CPI/core/PPI/salari vs target, sorprese, regime |
| Momentum crescita | 15% | PIL, PMI (con soglia 50), occupazione, retail |
| Sorprese recenti | 20% | tutte le sorprese, con decadimento esponenziale |
| Stance banca centrale | 15% | tono dei comunicati (semantico) |
| Saldo con l'estero | 5% | bilancia commerciale, partite correnti |

Pesi, soglie, half-life e target d'inflazione sono in
`backend/app/config/weights.yaml` e si ricaricano a caldo con
`POST /api/v1/admin/reload-config`.

**Bias di coppia** = (score base − score quotata) / 2, classificato in
STRONG_BULLISH / BULLISH / NEUTRAL / BEARISH / STRONG_BEARISH.

### Sorprese

`z = (actual − riferimento) / σ × direzione`, dove σ è stimata dallo storico
dell'indicatore (≥ 6 rilasci, escludendo l'evento stesso) e altrimenti dal
default dichiarato in `indicators.yaml`. La `direzione` ribalta gli indicatori
invertiti: disoccupazione sopra le attese produce un impulso **negativo**.
Classificazione: BIG_BEAT / BEAT / IN_LINE / MISS / BIG_MISS.

### Scenari

Per ogni coppia, sempre tre:

- **BASE** — il differenziale fondamentale resta il driver dominante;
- **ALT A** — sorpresa hawkish / risk-on sull'evento pivot imminente;
- **ALT B** — sorpresa dovish / risk-off (con il conflitto fra canale tassi e
  canale bene-rifugio dichiarato quando esiste).

Ogni scenario porta: probabilità (somma 100%), confidenza qualitativa,
magnitudo, catene causali attive, max 3 driver, condizioni di invalidazione,
eventi che possono riscriverlo, suggerimento operativo **non vincolante**,
timestamp e scadenza.

Vincoli di onestà, non negoziabili:

- probabilità dello scenario base **≤ 65%** (configurabile, mai rimossa);
- un evento RED entro 24h **abbassa la confidenza di un livello** e marca lo
  scenario come *riscrivibile*;
- fonti degradate → confidenza più bassa, non dati inventati;
- probabilità con **una sola cifra decimale**, sempre accompagnate dalla
  confidenza qualitativa.

---

## 4. Dashboard

| Pagina | Contenuto |
|---|---|
| `/` Overview | griglia valute con sotto-score e Δ24h, card coppie, prossimi eventi |
| `/calendario` | eventi filtrabili, sorpresa, catena causale e playbook al click |
| `/scenari` | i 3 scenari per coppia con driver, invalidazioni, catene, scadenza |
| `/banche-centrali` | stance, tasso di policy, ultimi documenti con sintesi in 2 frasi |
| `/affidabilita` | hit-rate, Brier score, curva di calibrazione, avviso campione |
| `/storico` | import storico Forex Factory, import prezzi, rigiocata del passato |
| `/fonti` | stato collector, job schedulati, **limiti dichiarati del sistema** |
| `/docs` | API interattive (FastAPI) |

Tema scuro, densità da terminale, zero build step: la dashboard è renderizzata
server-side (scelta motivata in `backend/app/web/routes.py`).

---

## 5. API

```
GET  /api/v1/currencies/scores            score compositi + sotto-score + catene
GET  /api/v1/currencies/{ccy}/score       dettaglio di una valuta
GET  /api/v1/pairs                        sintesi di tutte le coppie
GET  /api/v1/pairs/{symbol}/scenarios     3 scenari (?horizon=intraday|h24_72|w1_4)
GET  /api/v1/calendar?from=&to=&impact=   calendario con sorprese e playbook
GET  /api/v1/calendar/next-red            prossimi eventi RED
GET  /api/v1/central-banks                stance e documenti analizzati
GET  /api/v1/chains                       il modello causale, in chiaro
GET  /api/v1/playbooks                    playbook eventi
GET  /api/v1/reliability                  metriche di calibrazione
GET  /api/v1/sources                      stato fonti + limiti dichiarati
GET  /api/v1/health                       salute del sistema
GET  /api/v1/mt5/snapshot                 payload compatto per il bridge MQL5

POST /api/v1/admin/refresh                ricalcola score e scenari
POST /api/v1/admin/collect                esegue i collector
POST /api/v1/admin/rates                  aggiorna i tassi di policy
POST /api/v1/admin/calendar/import        import CSV/JSON di eventi
POST /api/v1/admin/calendar/import-mt5    storico dal terminale MT5 (consigliato)
POST /api/v1/admin/calendar/import-forexfactory   storico da pagina range=
POST /api/v1/admin/replay                 rigioca il passato e valuta gli scenari
POST /api/v1/admin/prices/import          import prezzi per la calibrazione
POST /api/v1/admin/evaluate               valuta ex-post gli scenari scaduti
POST /api/v1/admin/reload-config          ricarica i file YAML
POST /api/v1/admin/reclassify             riclassifica i documenti archiviati
```

Esempio — aggiornare un tasso di policy:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/admin/rates \
  -H "Content-Type: application/json" \
  -d '{"rates":[{"currency":"USD","rate":4.00,"central_bank":"FED"}]}'
```

---

## 6. Integrazione MetaTrader 5

Due modalità, entrambe supportate: file JSON in `MQL5\Files` (consigliata) o
WebRequest verso l'endpoint locale. Istruzioni complete, buffer esposti e
risoluzione problemi: **[`mql5/README_MQL5.md`](mql5/README_MQL5.md)**.

L'Expert Advisor **non apre posizioni**: blocca le aperture nelle finestre
evento, blocca i trade contro-bias, riduce il rischio quando la confidenza è
bassa e passa in modalità protettiva se i dati sono obsoleti. Parte in
**DRY-RUN**.

---

## 7. Layer semantico (opzionale)

Senza configurazione il tono dei comunicati è classificato da un dizionario
bilingue IT/EN pesato (`config/lexicon.yaml`) con gestione di negazioni e
intensificatori — completamente funzionante e testato.

Per usare un LLM al suo posto:

```ini
MSE_LLM_PROVIDER=anthropic
MSE_LLM_API_KEY=sk-ant-...
MSE_LLM_MODEL=claude-opus-5
```

Serve `pip install anthropic` (è opzionale, fuori dal `requirements.txt` standard
perché il sistema funziona benissimo senza). Se la chiamata fallisce
per qualsiasi motivo, il sistema **ricade automaticamente sul rule-based** e lo
dichiara nel log: nessuna analisi viene persa.

---

## 8. Calibrazione — dire la verità sui risultati

1. Esporta lo storico prezzi da MT5 in CSV (`pair,timestamp,close`).
2. `POST /api/v1/admin/prices/import` (o `/import-file`).
3. `POST /api/v1/admin/evaluate` — oppure lascia fare al job ogni 6 ore.

La pagina `/affidabilita` mostra hit-rate direzionale, Brier score
(riferimento casuale: 0.25) e curva di calibrazione a bucket. **Sotto i 30
scenari valutati compare l'avviso "campione insufficiente"** e le metriche non
vengono presentate come informative.

---

## 9. Test e simulazione end-to-end

```bash
cd macro-scenario-engine
backend/.venv/bin/python -m pytest          # 156 test
cd backend && .venv/bin/python demo_nfp.py  # FASE 9: NFP BIG_BEAT end-to-end
```

`demo_nfp.py` gira su un database temporaneo e mostra, in sequenza: evento
normalizzato → surprise score (con σ stimata dallo storico) → score USD prima e
dopo → scenario EURUSD prima e dopo → payload `/mt5/snapshot`. Esito verificato:
NFP 350K vs 180K → z = 5.0 BIG_BEAT → USD da +19.9 a +39.2 → EURUSD più
ribassista → XAUUSD BEARISH (effetto di secondo ordine dal playbook).

---

## 10. Limiti dichiarati

Sono mostrati anche in dashboard (`/fonti`) e via `GET /api/v1/sources`.

| Limite | Conseguenza e fallback |
|---|---|
| Forex Factory non ha un'API pubblica documentata. Si usa il feed JSON settimanale pubblico, che **non espone sempre il valore `actual`**. | Per gli storici degli `actual`: import CSV (`/admin/calendar/import`) oppure parser HTML di fallback, disattivato di default in `sources.yaml`. |
| Dei documenti delle banche centrali si analizza **titolo + sommario RSS**, non il testo integrale. | Scaricare ogni PDF violerebbe il rate limiting richiesto. L'analisi resta indicativa; il limite è dichiarato in dashboard. |
| I **tassi di policy** sono una tabella manuale (i valori di bootstrap in `sources.yaml` vanno verificati). | Aggiornabili via `POST /api/v1/admin/rates`. Oltre 45 giorni di anzianità il sotto-score perde qualità. |
| **XAU** non ha dati macro propri. | Score derivato come proxy inverso del dollaro (fattore 0.6), con la nota esplicita nel payload. Non include flussi fisici né premio geopolitico. |
| La **calibrazione richiede prezzi importati**. | Senza prezzi gli scenari non vengono valutati e la pagina Affidabilità lo dice. |
| Deduplica eventi su valuta + titolo + **giorno**. | Due eventi con titolo identico nello stesso giorno vengono fusi (accade solo per discorsi ripetuti). |
| Rate limiting: 1 richiesta / 5 minuti per fonte. | Se una fonte è irraggiungibile si usa la cache su disco, la fonte è marcata DEGRADED/DOWN e **la confidenza degli scenari scende**. |

---

## 11. Rispetto delle fonti

- user-agent dichiarato e configurabile (`MSE_USER_AGENT`);
- massimo **1 richiesta ogni 5 minuti per fonte** (configurabile al rialzo);
- cache su disco: una fonte non raggiungibile non genera raffiche di retry;
- retry con backoff esponenziale, massimo 3 tentativi;
- nessuno scraping di contenuti a pagamento;
- ogni esito è tracciato nello stato della fonte, visibile in dashboard.

---

## 12. Sicurezza

- nessuna chiave API nel codice: tutto in `.env`, con `.env.example` fornito;
- l'EA **non invia ordini** se non si disattiva esplicitamente il DRY-RUN;
- gli endpoint amministrativi sono pensati per un'installazione **locale**: se
  esponi il servizio in rete, mettilo dietro a un reverse proxy con
  autenticazione;
- disclaimer sempre visibile in dashboard e presente in ogni payload di scenario.
