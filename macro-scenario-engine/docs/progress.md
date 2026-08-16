# Avanzamento — Macro Currency Intelligence

## 2026-08-16 — CICLI 0–2

CICLO 0 — DISCOVERY: SUPERATO
  Input identificati: progetto esistente, exporter MQL5 + doc (hash registrati),
  Drive con FX Strength Desk V2. Constatazione: i due file storici NON esistono
  ancora (scripts/ su Drive vuota) — esiste il produttore, ora versionato.
  Output: docs/audit/00,01,02 + inventory.json.

CICLO 1 — AUDIT FX STRENGTH DESK: SUPERATO
  scoring.py e signals.py letti da Drive; 13 algoritmi catalogati con decisione
  ciascuno (integra/confronta/non toccare/scarta). Sorgenti restano su Drive.
  Output: docs/audit/03_fx_strength_desk_algorithms.md.

CICLO 2 — IMPORTER MT5: SUPERATO
  collectors/mt5_calendar.py: CSV/JSON/JSONL schema mt5-calendar-export/1,
  raw ×10^-6 senza perdita, time_utc→time_server con conteggio dichiarato,
  mappa importance dichiarata, report di copertura riconosciuto con messaggio
  utile, zero≠assente. Endpoint import-mt5[-file], pagina /storico sezione 1.
  Test: 15 nuovi, suite 139 verdi.

CICLI 3+ — BLOCCATI DAI DATI (non dal codice)
  Pilastri estesi, regime-aware weights, strength map benchmark USD, backtest
  point-in-time: richiedono lo storico reale. Comando utente: eseguire
  CalendarHistoryExporter.mq5 in MT5 e caricare l'output su /storico.

## 2026-08-16 — CICLO 3 (dati reali arrivati)

L'utente ha eseguito l'exporter: 209.309 record 2007→2026, 79/79 blocchi
completati, 0 falliti (report allegato + file su Drive: CSV 52MB, JSON/JSONL
139MB). declared_gmt_offset null → tutte le righe in ora server (osservato -9h).

- Import: aggiunto gmt_offset_hours (API + campo "Offset server" in /storico)
  con avviso esplicito sull'approssimazione da ora legale.
- Import: profilo di copertura FASE 3 calcolato al momento dell'import
  (per anno/valuta/impatto/categoria, actual/forecast/revisioni) e mostrato.
- docs/audit/06_dataset_profile_mt5.md dal report reale.
- Benchmark 209k righe (distribuzioni reali): pre-fix >10 min interrotto; a lotti 279,5 s, 191.876 sorprese, 749 eventi/s.
- Limite sandbox dichiarato: file completo non scaricabile qui (proxy 403,
  MCP inline 70MB); profiling completo gira nell'import sul PC dell'utente.

## 2026-08-16 — CICLO 4 (ricalibrazione sui numeri della rigiocata)

Verdetto misurato sul PC dell'utente (Fase D, campione 20.901): hit 20,9%,
Brier 0,224, LATERALE previsto 97,6% vs 20,5% reale, confidenza invertita.
Correzioni (vedi decision log): banda neutra adattiva vol×orizzonte con esiti
equiprobabili sotto il nullo, soglia direzione 8 separata dall'etichetta,
shrinkage probabilita' λ=0.5, LATERALE senza segnale mai sopra confidenza
BASSA (+ suggerimento STARE_FUORI prioritario). Dashboard Affidabilita':
nuova tabella direzioni previste/realizzate e banda dichiarata. Migrazione
schema additiva per la colonna neutral_band_pct sugli archivi esistenti.
Suite: 168 test verdi (12 nuovi in tests/test_ricalibrazione.py).

PROSSIMO PASSO (utente, PC locale): git pull sul branch, riavvio server,
rigiocata completa 2007→2026 sugli stessi dati, confronto con i numeri
sopra. Attesi: distribuzione direzioni prevista piu' vicina a quella reale,
calibrazione bucket con scarto ridotto, per_confidenza non piu' invertita.
I nuovi numeri decidono il prossimo giro di taratura.

## 2026-08-16 — CICLO 5 (strumento di misura per il secondo giro di taratura)

Secondo giro misurato (35.379 scenari): calibrazione risolta (+1,1 sul
bucket dominante), Brier 0,238 onesto (prima era basso "barando" con
probabilita' uniformi), laterale 75,1% previsto vs 42,1% reale. Restano:
hit direzionale 26-29% ≈ base rate (soglia 8 troppo aggressiva) e scala di
confidenza illeggibile per composizione. Aggiunte a /api/v1/reliability e
alla dashboard: per_bias (hit per fascia di |bias|) e per_confidenza
separata direzionali/laterali. Suite: 170 test verdi.

PROSSIMO PASSO (utente): aggiornare solo il codice (ZIP del branch),
riavviare, rileggere /api/v1/reliability — SENZA rifare la rigiocata — e
incollare per_bias + per_confidenza_direzionale/laterale. Da quei numeri:
scelta misurata della soglia di direzione ed eventuale ridefinizione del
punteggio di confidenza. Da tenere d'occhio anche il bias rialzista nelle
previsioni (RIALZO/RIBASSO 1,55x contro un realizzato simmetrico).
