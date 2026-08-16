# FASE 0/2 — Architettura effettiva del progetto

Reverse engineering completo già svolto e mantenuto durante lo sviluppo; qui
la mappa consolidata. Riferimenti dettagliati: README §1–§12.

## Flusso dati

```
Fonti (MT5 export storico ◄ CONSIGLIATA | FF browser | FF feed | RSS BC | CSV)
   └► collectors/  (calendar, ff_range, mt5_calendar, central_banks)
        └► storage/ (events, cb_documents, policy_rates, prices, scenarios…)
             └► engine/  surprise (z point-in-time, σ esclude l'evento stesso)
                         scoring  (6 sotto-score pesati, catene causali)
                         scenarios (3 per coppia, cap base 65%)
                  └► calibration/ (evaluation ex-post, replay con clock congelato)
                       └► web/ (dashboard), api/ (JSON), pipeline (snapshot MT5)
```

## Punti architetturali che vincolano l'integrazione

1. **Tutto il tempo passa da `repositories.now_utc()`** — congelabile con
   `clock_frozen_at()`: è la garanzia anti look-ahead della rigiocata. Nessun
   nuovo modulo deve chiamare `datetime.now()` direttamente.
2. **La conoscenza di dominio è in YAML** (`config/*.yaml`): pesi, playbook,
   lexicon, fonti, tassi bootstrap. I pesi regime-aware del prossimo ciclo
   vanno lì, versionati, non nel codice.
3. **`upsert_event` non cancella valori con un refresh vuoto**; chiave di
   dedup valuta+titolo+giorno (limite noto: due discorsi omonimi stesso
   giorno collassano; l'id esterno richiederebbe una colonna e migrazione).
4. **Ogni fonte scrive `source_health`** e la pagina `/fonti` la mostra:
   qualunque nuovo collector deve fare lo stesso (mt5_calendar lo fa).
5. **Il layer LLM è opzionale e ricade sul rule-based**: nessun modulo core
   può dipendere da una chiave API.

## Debito tecnico dichiarato

- Niente tabella vintages: `events.revised` tiene solo l'ultima revisione.
- Niente colonna `external_id`: dedup per contenuto, non per id fonte.
- Rigiocata: tassi ricostruiti solo dalle decisioni presenti nel calendario;
  cb_stance quasi sempre neutro nel passato (nessun comunicato storico).
- Pesi statici: il regime cambia il *regime di inflazione* ma non ripesa i
  pilastri (candidato CICLO 3 con benchmark obbligatorio).
