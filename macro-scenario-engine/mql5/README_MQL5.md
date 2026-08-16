# MQL5 Scenario Bridge — installazione e uso

Componente B del Macro Scenario Engine: porta su MetaTrader 5 gli scenari
calcolati dal backend Python.

| File | Cosa fa |
|---|---|
| `Include/MacroScenarioTypes.mqh` | strutture dati + parser JSON dedicato allo schema `/mt5/snapshot` |
| `Indicators/MacroScenarioBridge.mq5` | pannello grafico + 4 buffer riutilizzabili |
| `Experts/MacroScenarioEA.mq5` | livello di **filtro e gestione del rischio** (non è un entry system) |

---

## 1. Installazione

In MetaTrader 5: **File → Apri cartella dati**, poi copia:

```
MQL5\Include\MacroScenarioTypes.mqh
MQL5\Indicators\MacroScenarioBridge.mq5
MQL5\Experts\MacroScenarioEA.mq5
```

In MetaEditor premi **F7** (Compila) su ciascun `.mq5`.

**Warning attesi in compilazione:** nessuno. Se MetaEditor segnala
`declaration of 'reason' hides global declaration` o simili su versioni molto
vecchie del compilatore, si tratta di avvisi informativi e non bloccanti.
Errori di tipo `cannot open source file MacroScenarioTypes.mqh` significano
che l'include non è in `MQL5\Include`.

---

## 2. Scegliere la modalità di alimentazione dati

### (b) File JSON — **consigliata per iniziare**

Nessuna autorizzazione da configurare in MT5.

1. Nel file `backend/.env` imposta la cartella `Files` del terminale:

   ```ini
   MSE_MT5_FILES_DIR=C:\Users\NOME\AppData\Roaming\MetaQuotes\Terminal\<ID>\MQL5\Files
   ```

   (il percorso esatto è quello che si apre con **File → Apri cartella dati**,
   sotto `MQL5\Files`).

2. Il backend, quando è in esecuzione, riscrive `macro_snapshot.json` ogni
   minuto. In alternativa, senza avviare il server:

   ```bash
   python export_mt5_file.py --loop 60 --refresh
   ```

3. Nell'indicatore e nell'EA lascia `InpSource = MSE_SOURCE_FILE` e
   `InpFileName = macro_snapshot.json`.

La scrittura del file è **atomica** (file temporaneo + rinomina): MT5 non legge
mai un file a metà.

### (a) WebRequest — endpoint locale

1. In MT5: **Strumenti → Opzioni → Expert Advisors**, spunta
   *Consenti WebRequest per gli URL elencati* e aggiungi:

   ```
   http://127.0.0.1:8000
   ```

   > L'URL va inserito **senza** il percorso (`/api/v1/...`): MT5 autorizza per host.

2. Imposta `InpSource = MSE_SOURCE_WEB` e verifica che
   `InpUrl = http://127.0.0.1:8000/api/v1/mt5/snapshot`.

3. Se vedi in log `URL non autorizzato: aggiungerlo in Strumenti > Opzioni`,
   l'URL non è nella whitelist (errore MT5 4014).

---

## 3. Indicatore `MacroScenarioBridge`

Pannello sul grafico (oggetti `OBJ_`, non `Comment()`):

- bias della coppia con colore e freccia;
- le tre probabilità di scenario con barra orizzontale;
- livello di confidenza e scadenza dello scenario;
- countdown al prossimo evento RED sulle valute della coppia;
- badge **NO-TRADE** quando è attiva una finestra di blocco;
- avviso esplicito se i dati sono più vecchi di `InpStaleMinutes`.

### Buffer esposti

| Buffer | Contenuto |
|---:|---|
| 0 | bias di coppia, da −100 a +100 |
| 1 | probabilità dello scenario base (%) |
| 2 | minuti al prossimo evento RED (−1 se nessuno) |
| 3 | flag no-trade (0 / 1) |

Uso da un altro EA:

```mql5
int handle = iCustom(_Symbol, PERIOD_CURRENT, "MacroScenarioBridge");
double bias[1], prob[1], red[1], notrade[1];

CopyBuffer(handle, 0, 0, 1, bias);
CopyBuffer(handle, 1, 0, 1, prob);
CopyBuffer(handle, 2, 0, 1, red);
CopyBuffer(handle, 3, 0, 1, notrade);

if(notrade[0] > 0.5)
   return;                     // finestra evento: non aprire
if(bias[0] < -40)
   return;                     // niente long contro-bias
```

Quando i dati sono obsoleti l'indicatore forza `bias = 0`, `prob = 0` e
`no-trade = 1`: chi legge i buffer resta protetto anche senza controllare l'età.

### Refresh

`InpRefreshSeconds` (default 60 s). Entro `InpRedWindowMinutes` da un evento
RED il refresh diventa più frequente automaticamente.

---

## 4. Expert Advisor `MacroScenarioEA`

**Non apre posizioni.** È un livello di filtro e gestione del rischio.
Parte in **DRY-RUN**: logga le decisioni senza inviare ordini. Il passaggio a
live è l'input esplicito `InpDryRun = false`.

Cosa fa:

- blocca nuove aperture nelle finestre pre/post evento RED
  (`InpBlockMinutesBefore` / `InpBlockMinutesAfter`, default 30 / 15);
- blocca i trade contro-bias oltre `InpBlockAgainstBiasAbove` (default 40);
- riduce il rischio quando la confidenza dello scenario è MEDIA o BASSA;
- avvisa (log / Alert / notifica push) quando lo scenario viene rigenerato,
  scade o diventa "riscrivibile";
- **modalità protettiva**: se i dati sono più vecchi di `InpStaleMinutes`
  (default 30) nessuna nuova apertura è consentita e lo stato lo dichiara;
- opzionalmente chiude le posizioni prima di un evento RED
  (`InpCloseBeforeRed`, disattivo di default e ignorato in DRY-RUN).

### Integrazione con la tua strategia

Compila la tua strategia insieme a questo file e chiama:

```mql5
string reason;
if(!MseTradeAllowed(+1, reason))     // +1 long, -1 short
  {
   Print("Ingresso saltato: ", reason);
   return;
  }

double risk = MseSuggestedRiskPercent();   // rischio base x fattore confidenza
double lots = CalcolaLotti(risk);          // la tua funzione di sizing
```

`MseRiskFactor()` restituisce 0 quando i dati non sono utilizzabili: un sizing
che moltiplica per questo fattore si azzera da solo invece di operare al buio.

---

## 5. Risoluzione problemi

| Sintomo | Causa e rimedio |
|---|---|
| `File macro_snapshot.json non leggibile` | Il backend non ha ancora esportato, oppure `MSE_MT5_FILES_DIR` punta alla cartella sbagliata. Verifica con `python export_mt5_file.py`. |
| `URL non autorizzato` (errore 4014) | Aggiungi `http://127.0.0.1:8000` in Strumenti → Opzioni → Expert Advisors. |
| `Versione schema N non supportata` | Il backend è più recente del bridge: aggiorna i file MQL5 da questa cartella. |
| `La coppia XXXYYY non e' configurata nel backend` | Aggiungi il simbolo in `backend/app/config/operator.yaml` → `coppie`, poi `POST /api/v1/admin/reload-config`. |
| `Snapshot senza coppie` | Il backend non ha ancora calcolato nulla: lancia `POST /api/v1/admin/refresh`. |
| Pannello con `DATI OBSOLETI` | Il backend è fermo o l'export non gira. In questo stato l'EA blocca le aperture per scelta. |
| Simbolo del broker con suffisso (`EURUSD.m`) | Gestito: il bridge normalizza il simbolo ai primi 6 caratteri alfanumerici. |

---

## 6. Limiti dichiarati

- Il bridge **non fa analisi**: mostra ciò che il backend ha calcolato. Se il
  backend ha fonti degradate, lo scenario ha confidenza più bassa — è indicato
  nel pannello e nell'elenco `fonti_degradate` dello snapshot.
- Il parser JSON è dedicato allo schema `v: 1`. Non gestisce sequenze di escape:
  il backend ripulisce le stringhe alla fonte. Un cambio di schema viene
  **segnalato**, non indovinato.
- Le probabilità sono stime di modello, con cap strutturale al 65% sullo
  scenario base. Non sono garanzie e non costituiscono consulenza finanziaria.
