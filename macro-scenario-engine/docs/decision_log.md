# Decision log

2026-08-16 · Fonte storica canonica = export del terminale MT5 (offline,
  profonda, con actual e revisioni), non Forex Factory ne' FRED: nessuna
  chiave API, nessun accesso di rete, riproducibile dall'utente.

2026-08-16 · I sorgenti del FX Strength Desk NON vengono copiati nel repo:
  catalogati in docs/audit/03 con riferimenti Drive. Motivo: evitare due
  codebase divergenti nello stesso repo; si integrano le idee, non i file.

2026-08-16 · Mappa importance MetaQuotes→impatto interno dichiarata
  (high→RED, moderate→ORANGE, low→YELLOW, none/holiday→GRAY) e stampata in
  ogni report di import: la doc del produttore vieta di mapparla in silenzio.

2026-08-16 · Valori numerici presi dai campi *_raw (×10^-6), non dai campi
  formattati: precisione piena, come raccomandato dal produttore.

2026-08-16 · Niente tabella vintages in questo ciclo: aggiungerla senza dati
  reali su cui testarla sarebbe speculazione. events.revised tiene l'ultima
  revisione; limite scritto in 02_dataset_identification.md.

2026-08-16 · Backup/rollback = git (branch + commit + push): il repo e' gia'
  protetto; una copia .backups/ duplicherebbe il VCS. Istruzioni in README.

2026-08-16 · CICLO 4 — Ricalibrazione misurata (rigiocata 2007-2026,
  20.901 scenari valutati sul PC dell'utente). Quattro difetti misurati,
  quattro correzioni, ognuna con il numero che la giustifica:
  (1) banda neutra fissa ±0.15% → adattiva: ±0.4307·σ_giornaliera·√giorni,
      cosi' sotto un random walk i tre esiti sono equiprobabili (~33%).
      Misurato: con la banda fissa l'80,5% degli esiti multi-giorno era
      direzionale e LATERALE quasi irrealizzabile. σ stimata SOLO su barre
      precedenti alla generazione (nessun lookahead nella soglia); pavimento
      ±0.05%; riserva fissa ±0.15% sotto 20 barre, dichiarata nel report.
  (2) soglia direzione separata (soglie_bias.direzione=8) dall'etichetta
      (normale=15). Misurato: LATERALE previsto 97,6% contro ~20% reale;
      le chiamate direzionali fatte centravano il 37-42%.
  (3) shrinkage_lambda=0.5: probabilita' compresse verso 1/3 a somma
      invariata. Misurato: dichiarato ~45% contro frequenza reale ~21%
      (scarto -24 punti su 20.885 casi nel bucket 40-50%).
  (4) direzione LATERALE senza segnale → confidenza mai sopra BASSA.
      Misurato: confidenza invertita (ALTA 18,2% < BASSA 23,7%) perche'
      l'ALTA si concentrava sui laterali, la classe peggiore.
  Tutti e quattro i parametri sono in weights.yaml e dichiarati nel payload
  di ogni scenario ("calibrazione") e nel report /api/v1/reliability
  ("banda_neutra"): rigiocate con parametri diversi non sono confrontabili
  e il sistema lo dice. Nessuno di questi numeri e' definitivo: vanno
  rimisurati con una nuova rigiocata (stesso archivio, stessi prezzi).

2026-08-16 · Migrazione schema additiva in init_db (_migrate_schema):
  ALTER TABLE ADD COLUMN per le sole colonne opzionali mancanti, cosi'
  l'archivio esistente da 122MB dell'utente si aggiorna in place senza
  reimportare nulla. Colonne obbligatorie senza default → errore esplicito,
  mai valori inventati.

2026-08-16 · CICLO 5 — Verdetto del secondo giro (35.379 scenari, banda
  adattiva attiva su tutti): calibrazione RISOLTA (bucket dominante scarto
  +1,1 contro -24,2), monocultura laterale rientrata (97,6%→75,1%), ma le
  chiamate direzionali con soglia 8 hanno hit 26-29% ≈ frequenza di base:
  la soglia abbassata ha diluito il margine che a 15 c'era (37-42%). La
  tabella per_confidenza marginale e' risultata fuorviante per composizione
  (i laterali sono BASSA per regola: 89% del campione). Decisione: NESSUN
  ritocco di parametri a occhio; si costruisce lo strumento di misura —
  per_bias (hit per fascia di |bias|, per scegliere la soglia dai dati) e
  per_confidenza_direzionale/laterale (scala giudicata a parita' di classe).
  Le nuove metriche leggono gli esiti gia' salvati: nessuna rigiocata
  necessaria per ottenerle.
