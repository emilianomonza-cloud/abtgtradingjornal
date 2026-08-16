# FASE 2 — Catalogo algoritmi: FX Strength Desk V2 (Drive)

Fonte: `fx_production/api/{scoring,signals}.py` + handoff
`FX_STRENGTH_DESK_HANDOFF_CLAUDE_CODE_1.docx` (Drive, cartella condivisa
dell'utente). I sorgenti restano su Drive come fonte canonica; qui sono
catalogate le formule con la decisione di integrazione per ciascuna.

Contesto: il Desk è un progetto separato (React+FastAPI, scala 0–100,
USD-centrico, dati FRED/ECB/BOC) nato da una spec "Kimi" e già passato per un
audit di 23 bug. Non va fuso alla cieca col Macro Scenario Engine (scala
−100/+100, event-driven): vanno innestate le idee compatibili.

## USDScoringEngine (scoring.py)

| # | Algoritmo | Formula | Giudizio | Decisione |
|---|---|---|---|---|
| 1 | Pesi pilastri USD | inflation .25, labor .20, growth .15, rates .25, fed_stance .15 — «approvati, non cambiare» | Coerenti coi nostri (25/20/15/25/15 vs nostri 25/20/15/20/15+5) | **Confrontare**, non sostituire: i nostri pesi sono in YAML versionato |
| 2 | Normalizzazione lineare | `clamp((x−min)/(max−min)×100)` con range fissi per indicatore (CPI −1..8, NFP −200..400, ISM 45..65…) | Semplice, spiegabile; range hardcoded = fragilità dichiarata | **Integrare come alternativa** alla z-score quando lo storico è corto (<6 rilasci) |
| 3 | EMA per orizzonte | α = {1W:.50, 1M:.33, 3M:.18}; EMA su score composito | Buona idea: smoothing dello score, non dei dati | **Candidata**: nostro `score_change_24h` è più povero |
| 4 | Momentum reale | delta score vs N osservazioni fa (7/30/90); trend ±2.0 | Corretto (fixato dal random) | Già coperto in forma diversa; tenere come cross-check |
| 5 | Real rate | `FED_FUNDS − CORE_PCE` normalizzato −2..4 | Solido, manca nel nostro rate_differential | **Integrare** quando avremo serie inflazione dallo storico MT5 |
| 6 | Confidence | `min(75, 60 + non_null×1.5)` — cap 75% dichiarato compliance | Il cap è un vincolo di onestà affine ai nostri | Nostro equivalente: cap 65% probabilità base + confidence qualitativa. Nessun cambio |
| 7 | Fed stance proxy | `(fed_funds − core_pce)×10` in assenza di NLP | Fallback dichiarato | Nostro rule-based lexicon è più ricco. Nessun cambio |

## PeerScoringEngine (scoring.py)

| # | Algoritmo | Formula | Decisione |
|---|---|---|---|
| 8 | Peer score | media pesata di inflazione (.25), disoccupazione inversa (.20), GDP (.15), policy_rate/6% (.25), PMI (.15) su range G10 | Stessa filosofia dei nostri sotto-score; scala diversa (0–100 vs ±100). **Non sostituire**; utile come benchmark nel futuro backtest |

## SignalGenerator (signals.py)

| # | Algoritmo | Formula | Decisione |
|---|---|---|---|
| 9 | Edge | `usd_score − peer_score` | Identico al nostro `bias = score(base) − score(quote)` generalizzato a qualsiasi coppia. Già coperto |
| 10 | Direction XXXUSD/USDXXX | edge>+20 & usd>50 → USD bullish → SHORT su XXXUSD, LONG su USDXXX | **Regola semanticamente giusta** (evita il "short Japan USD"). Il nostro pair engine la implementa già via base/quote; aggiunto test di semantica al prossimo ciclo pair-ranking |
| 11 | Conviction | `min(|edge|,50)×2 + 10 se usd estremo + 10 se DXY allineato`, cap 100 | Ragionevole ma con catalyst/invalidation **hardcoded demo** (FOMC 2024!) | Non integrare: i nostri driver/invalidazioni derivano dai dati |
| 12 | DXY coherence | edge>15 & DXY up → aligned, ecc. | Idea buona (sanity check esterno) | Candidata: richiede una serie DXY (proxy FRED DTWEXBGS) — dopo i dati |
| 13 | Soglia neutralità | `|edge| < 20 → NEUTRAL` | Compatibile con la nostra banda LATERALE | Confronto in backtest |

## Regime (handoff §4.2/4.4 — spec, implementazione parziale nel Desk)

Regole proposte: inflation high>3.5%/above_target>2.5%; risk_off VIX>25;
growth ISM><50; pesi che si spostano (+infl −growth in regime inflattivo).
**Decisione**: è la candidata principale per il CICLO 3 del Macro Scenario
Engine (pesi regime-aware in `weights.yaml`, già predisposto ai regimi
HAWKISH_DOMINANT/CREDIBILITY_LOSS) — MA solo quando lo storico MT5 reale
permetterà di testare i pesi contro il backtest point-in-time, come impone il
principio "non promuovere un modello senza benchmark".

## Cosa NON portare dentro (con motivo)

- Catalyst/invalidation hardcoded (`signals.py`): dati demo del 2024 spacciabili
  per analisi. Il nostro sistema li genera dai dati.
- Dipendenza FRED come fonte primaria: chiave API + rete; la nostra fonte
  primaria storica è il terminale MT5 dell'utente, offline.
- Mock 71KB del frontend: numeri decorativi, contrari ai principi del progetto.
