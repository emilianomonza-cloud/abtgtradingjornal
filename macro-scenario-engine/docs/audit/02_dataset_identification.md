# FASE 0/3 — Identificazione dei dataset storici

## I due file: cosa sono davvero

La missione presuppone due file storici macroeconomici nella directory.
La scansione ha stabilito che **non sono ancora stati generati**: esiste il
loro produttore, `mql5/Scripts/CalendarHistoryExporter.mq5`, che l'utente deve
eseguire nel proprio terminale MT5. Produrrà, per ogni run:

| File | Formato | Ruolo assegnato |
|---|---|---|
| `mt5_calendar_<da>_<a>.csv` | CSV UTF-8 BOM, 29 colonne | **Fonte canonica** dello storico eventi (stessi record del JSON) |
| `mt5_calendar_<da>_<a>.json` / `.jsonl` | array / righe JSON | Equivalente senza perdita; preferito per pipeline |
| `mt5_calendar_<da>_<a>_report.json` | oggetto JSON | **Report di copertura**: non contiene eventi, contiene la verità su cosa manca |

Classificazione empirica del ruolo (da schema del produttore, sorgente
versionata nel repo): CSV e JSON sono **lo stesso archivio in due formati**,
non raw+normalizzato né periodi diversi. La deduplica canonica è `value_id`
(univoco per valore nel terminale). Il report è la fonte di verità sulla
completezza: `chunks[]` distingue `completed` / `empty_verified` /
`failed_explained` — un periodo vuoto non è un periodo non acquisito.

## Schema canonico dei campi (dal produttore, non ipotizzato)

- Valori numerici: interi del terminale ×10⁶ nei campi `*_raw` (senza perdita);
  campi formattati già divisi e arrotondati a `digits`. **L'import usa i raw.**
- Assenza = `null`/cella vuota. Mai zero. `0.00` è uno zero vero (es. tasso BCE).
- `time_server` sempre presente (fuso del server broker); `time_utc` solo se
  l'offset è stato dichiarato nell'exporter. Priorità di import: `time_utc`,
  poi `time_server` con conteggio dichiarato delle righe non convertite.
- `revision` = numero di revisione del valore; `revised_previous` = precedente
  rivisto → `events.revised`. Prima pubblicazione vs revisioni: lo schema
  attuale NON conserva le vintage complete (limite dichiarato, estensione
  candidata quando ci saranno i dati per testarla).
- `importance` (none/low/moderate/high) è la tassonomia MetaQuotes. Mappa
  dichiarata verso l'impatto interno: high→RED, moderate→ORANGE, low→YELLOW,
  none e holiday→GRAY. Da verificare sulla distribuzione reale al primo import.
- `impact_type` (na/positive/negative) è la lettura MetaQuotes della sorpresa:
  conservato come input futuro per la verifica della polarità, non usato oggi
  al posto della mappa di polarità interna.

## Fonti ausiliarie individuate (non eliminate, non canoniche)

1. **Estrazione Forex Factory dal browser** (`collectors/ff_range.py`,
   `strumenti/`): copre i periodi che il broker non ha; deduplica per id FF.
2. **Feed settimanale FF + RSS banche centrali** (collector automatici): flusso
   corrente, non storico.
3. **FX Strength Desk V2 su Drive**: non è un dataset ma un progetto; le sue
   formule sono catalogate in `03_fx_strength_desk_algorithms.md`.

## Decisione di integrazione

- Fonte canonica storica: export MT5 (quando esisterà), import via
  `/api/v1/admin/calendar/import-mt5[-file]` o pagina `/storico` sezione 1.
- Conflitti fra fonti sullo stesso evento (stessa valuta+titolo+giorno):
  l'ultimo import aggiorna i valori presenti senza cancellare quelli assenti
  (regola già attiva in `upsert_event`: un refresh senza actual non azzera
  l'actual salvato).
- Record senza metadati evento risolti (`event_lookup_failures` del produttore):
  scartati e **contati** nel report di import, mai importati mutilati.
