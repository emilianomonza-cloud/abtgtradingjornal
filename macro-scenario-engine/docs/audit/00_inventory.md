# FASE 0 — Inventario degli input

Data audit: 2026-08-16 · Branch: `claude/macro-scenario-engine-forex-pz6d08`

## Cosa è stato trovato

### 1. Progetto nella directory di lavoro

`macro-scenario-engine/` — piattaforma di analisi fondamentale FX già operativa:
collector (Forex Factory + RSS banche centrali), catene causali esplicite,
scoring a 6 sotto-score, scenari probabilistici con cap 65%, calibrazione
ex-post (hit-rate, Brier), rigiocata storica con orologio congelato, dashboard
server-side, bridge MQL5. 139 test verdi. Architettura completa in
`01_project_architecture.md`.

### 2. File allegati alla sessione (hash SHA-256 misurati)

| File | SHA-256 | Ruolo |
|---|---|---|
| `CalendarHistoryExporter_1.mq5` | `b21f459aeda85c06d339d50632ba9ac7a5cab98658e90bccaeba2103683f38e7` | **Produttore** dei file storici: esporta il calendario del terminale MT5 in CSV/JSON/JSONL (schema `mt5-calendar-export/1`) |
| `CalendarHistoryExporter_DOC.md.docx` | `5c7396d607c1cad63c92e0555700d2efe0781a6d965799d43413221bd2b3863f` | Documentazione del produttore: semantica campi, limiti, fusi orari |

Entrambi versionati in `mql5/Scripts/` (il .docx convertito in `.md`).

### 3. Cartella Google Drive (link fornito dall'utente)

```
Drive/
├── FX_STRENGTH_DESK_HANDOFF_CLAUDE_CODE_1.docx   ← handoff di un TERZO progetto
└── fx_production/                                 ← FX Strength Desk V2.0 (feb 2026)
    ├── api/    main.py, scoring.py, signals.py, data_fetcher.py  (FastAPI)
    ├── app/    frontend React+Vite+ShadCN (TypeScript)
    └── scripts/                                   ← VUOTA
```

## Constatazione centrale — dichiarata, non aggirata

**I "due file storici macroeconomici" non esistono ancora.** La cartella
`scripts/` su Drive è vuota; nella directory di lavoro non c'è nessun dataset
CSV/JSON/JSONL/Parquet/SQLite/XLSX con dati macro storici (verificato con
scansione ricorsiva). Ciò che esiste è lo **strumento che li produrrà**:
l'exporter MQL5 va eseguito nel terminale MT5 dell'utente (il calendario è un
database locale del terminale, non raggiungibile da qui).

Conseguenze sul piano di lavoro:

- Le FASI 0–2 (discovery, protezione, reverse engineering) sono complete.
- La FASE 3 (profiling dei due file) è **bloccata dai dati**, non dal codice:
  l'intera catena di import → normalizzazione → sorprese → score → scenari →
  rigiocata → calibrazione è implementata e testata su campioni che replicano
  esattamente lo schema del produttore (che è versionato nel repo).
- Appena i file esistono, il percorso è: pagina `/storico` → sezione 1 →
  carica il file. Nessun passo intermedio.

## Rischi iniziali registrati

1. **Fuso orario dell'export**: `time_utc` è null se l'utente non dichiara
   l'offset del server. L'import usa `time_server` e lo dichiara; errore
   massimo: ore, non giorni. → `05_risk_register` in questo file, punto sotto.
2. **`importance` MetaQuotes ≠ impatto Forex Factory**: mappa dichiarata
   high→RED, moderate→ORANGE, low→YELLOW, none/holiday→GRAY. Andrà verificata
   sul dataset reale (distribuzione in `by_importance` del report exporter).
3. **Vintage delle revisioni**: lo schema `events` attuale tiene l'ultima
   versione nota (`revised`), non la storia completa delle revisioni. Il campo
   `revision` dell'export lo permetterebbe: estensione candidata al prossimo
   ciclo, da fare CON i dati reali davanti.
4. **Profondità storica dipendente dal broker**: il report di copertura
   dell'exporter (chunks completed/empty/failed, by_year) è l'unica verità;
   non assumere copertura uniforme.
