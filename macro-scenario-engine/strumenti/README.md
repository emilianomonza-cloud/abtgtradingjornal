# Strumenti offline

Due file che funzionano **senza il server acceso**, nel solo browser.

| File | A cosa serve |
|---|---|
| `estrai_forexfactory.js` | si incolla nella Console sulla pagina del calendario e scarica gli eventi in `.json` |
| `storico.html` | si apre con doppio clic: unisce piu' file, filtra, ordina ed esporta CSV o un `.json` unico |

## Flusso consigliato

1. Estrai un range alla volta con `estrai_forexfactory.js`
   (`?range=aug1.2019-oct2.2019`, poi il trimestre dopo, e cosi' via).
2. Trascina tutti i `.json` dentro `storico.html`: la deduplica avviene
   sull'`id` evento di Forex Factory, stabile fra range diversi, quindi i
   periodi sovrapposti non creano doppioni.
3. Controlla il dataset — quanti eventi, quali valute, quanti con `actual`.
4. **Salva dataset (.json)** e carica quel file unico nella pagina `/storico`
   del Macro Scenario Engine.

Il passaggio 3 non e' burocrazia: e' il momento in cui vedi se un periodo e'
mezzo vuoto prima che finisca in archivio e sporchi le stime.

## Cosa NON fanno

Nessuno dei due contatta la rete. `storico.html` tiene tutto in memoria: se
chiudi la scheda senza salvare, il dataset unito si perde (i file sul disco
restano). E' una scelta, non una mancanza — nessun dato di navigazione viene
scritto da qualche parte a tua insaputa.

## Perche' la tabella HTML non viene letta

Misurato sulla pagina reale: la tabella e' renderizzata in modo pigro e in una
pagina salvata compaiono circa **52 righe su 801 eventi**. Leggerla
importerebbe il 7% dei dati dichiarando successo. Sia questi strumenti sia il
backend rifiutano quel percorso e restituiscono un errore col motivo.
