"""SEZIONE 7 — Valutazione ex-post degli scenari e metriche di affidabilita'.

Nessuna metrica viene mostrata come definitiva sotto le 30 valutazioni: il
sistema dichiara esplicitamente "campione insufficiente".
"""

from __future__ import annotations

import csv
import io
import logging
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..storage.models import PriceBar, Scenario, ScenarioOutcome
from ..storage.repositories import naive_utc, now_utc

logger = logging.getLogger(__name__)

# Banda neutra di riserva (in %): usata solo quando mancano abbastanza barre
# per stimare la volatilita' della coppia. Era la banda fissa originale.
NEUTRAL_BAND_PCT = 0.15
# Pavimento della banda adattiva: con una banda quasi nulla nessun esito
# sarebbe mai LATERALE e la classe sparirebbe dal campione.
NEUTRAL_BAND_MIN_PCT = 0.05
# Barre giornaliere usate per la volatilita' trascinata, e minimo per fidarsi.
VOL_LOOKBACK_BARS = 60
VOL_MIN_BARS = 20
# P(|N(0,1)| < 0.4307) = 1/3: con questa banda, sotto un random walk, i tre
# esiti (rialzo/ribasso/laterale) sono equiprobabili. La banda fissa di 0.15%
# non lo era: misurato su 20.901 scenari rigiocati (2007-2026), su orizzonti
# multi-giorno classificava direzionale l'80% dei casi, rendendo LATERALE
# quasi impossibile da realizzare ma facilissimo da prevedere.
EQUIPROBABLE_Z = 0.4307
# Tolleranza nella ricerca del prezzo piu' vicino a un istante (ore).
PRICE_TOLERANCE_HOURS = 12
# Campione minimo per considerare le metriche informative.
MIN_SAMPLE = 30

RIALZO = "RIALZO"
RIBASSO = "RIBASSO"
LATERALE = "LATERALE"


# --------------------------------------------------------------------------- #
#  Prezzi
# --------------------------------------------------------------------------- #


def import_prices_csv(session: Session, payload: str, default_pair: str = "") -> int:
    """Import prezzi da CSV.

    Due formati accettati:
      * generico: colonne `pair` (opzionale se `default_pair`), `timestamp|date`,
        `close`, separate da virgola;
      * export nativo di MT5 ("Esporta barre" da Visualizza -> Simboli):
        separato da TAB, intestazioni fra parentesi angolari (<DATE>, <TIME>,
        <CLOSE>, ...), date in formato YYYY.MM.DD. Non contiene il simbolo:
        serve `default_pair` (la casella "Coppia" nella pagina /storico).

    Il secondo formato e' quello che il terminale produce davvero: senza
    questo riconoscimento l'import restituirebbe zero righe in silenzio.
    """
    payload = payload.lstrip("﻿")
    prima_riga = payload.split("\n", 1)[0]
    if "\t" in prima_riga:
        delimitatore = "\t"
    elif ";" in prima_riga and "," not in prima_riga:
        delimitatore = ";"
    else:
        delimitatore = ","

    reader = csv.DictReader(io.StringIO(payload), delimiter=delimitatore)
    # <DATE> -> date: le parentesi angolari dell'export MT5 vengono spogliate
    # una volta sola, sui nomi colonna.
    if reader.fieldnames:
        reader.fieldnames = [
            (f or "").strip().strip("<>").strip() for f in reader.fieldnames
        ]
    inserted = 0
    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        pair = (row.get("pair") or row.get("symbol") or default_pair).upper()
        stamp = row.get("timestamp") or row.get("timestamp_utc") or row.get("date") or ""
        close_raw = row.get("close") or row.get("price") or ""
        if not pair or not stamp or not close_raw:
            continue

        moment = _parse_price_datetime(stamp, row.get("time"))
        if moment is None:
            continue
        try:
            close = float(close_raw.replace(",", "."))
        except ValueError:
            continue

        existing = session.execute(
            select(PriceBar).where(PriceBar.pair == pair, PriceBar.timestamp_utc == moment)
        ).scalar_one_or_none()
        if existing is not None:
            existing.close = close
            continue
        session.add(PriceBar(pair=pair, timestamp_utc=moment, close=close, source="csv"))
        inserted += 1
    return inserted


def _parse_price_datetime(stamp: str, time_part: Optional[str] = None) -> Optional[datetime]:
    text = stamp.strip()
    if time_part:
        text = f"{text} {time_part.strip()}"
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d %H:%M",
        "%Y-%m-%d",
        "%Y.%m.%d",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return naive_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def price_at(session: Session, pair: str, moment: datetime) -> Optional[float]:
    """Prezzo di chiusura piu' vicino all'istante richiesto, entro la tolleranza."""
    moment = naive_utc(moment)
    window = timedelta(hours=PRICE_TOLERANCE_HOURS)
    rows = list(
        session.execute(
            select(PriceBar)
            .where(
                PriceBar.pair == pair.upper(),
                PriceBar.timestamp_utc >= moment - window,
                PriceBar.timestamp_utc <= moment + window,
            )
            .order_by(PriceBar.timestamp_utc.asc())
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    closest = min(rows, key=lambda r: abs((r.timestamp_utc - moment).total_seconds()))
    return closest.close


# Quanto in avanti cercare la prima chiusura disponibile quando l'orizzonte
# finisce a mercato chiuso. 80 ore coprono un weekend piu' un festivo.
END_PRICE_FORWARD_HOURS = 80


def price_at_or_next(session: Session, pair: str, moment: datetime) -> Optional[float]:
    """Prezzo alla FINE di un orizzonte: il piu' vicino, o la prima chiusura dopo.

    Un orizzonte che scade sabato o domenica non ha una barra giornaliera entro
    la tolleranza: senza questa ricerca in avanti tutti gli scenari generati di
    venerdi' restavano non valutati per sempre (misurato: 373 su 1.827 in un
    anno di rigiocata, cioe' i venerdi' per 7 coppie). Usare la chiusura del
    lunedi' e' la scelta onesta: il gap del weekend E' movimento di mercato che
    lo scenario doveva coprire, non rumore da scartare.
    """
    vicino = price_at(session, pair, moment)
    if vicino is not None:
        return vicino
    moment = naive_utc(moment)
    prossima = session.execute(
        select(PriceBar)
        .where(
            PriceBar.pair == pair.upper(),
            PriceBar.timestamp_utc > moment,
            PriceBar.timestamp_utc <= moment + timedelta(hours=END_PRICE_FORWARD_HOURS),
        )
        .order_by(PriceBar.timestamp_utc.asc())
        .limit(1)
    ).scalars().first()
    return prossima.close if prossima is not None else None


# --------------------------------------------------------------------------- #
#  Valutazione
# --------------------------------------------------------------------------- #


def trailing_sigma_pct(session: Session, pair: str, moment: datetime) -> Optional[float]:
    """Deviazione standard dei rendimenti giornalieri (%) fino a `moment`.

    Usa SOLO chiusure con timestamp <= moment: la soglia con cui si giudica uno
    scenario e' calcolata sugli stessi dati che erano disponibili quando lo
    scenario e' stato generato, senza lookahead. Restituisce None quando le
    barre sono meno di VOL_MIN_BARS: in quel caso si usa la banda fissa.
    """
    moment = naive_utc(moment)
    closes = list(
        session.execute(
            select(PriceBar.close)
            .where(PriceBar.pair == pair.upper(), PriceBar.timestamp_utc <= moment)
            .order_by(PriceBar.timestamp_utc.desc())
            .limit(VOL_LOOKBACK_BARS)
        )
        .scalars()
        .all()
    )
    if len(closes) < VOL_MIN_BARS:
        return None
    closes.reverse()
    returns = [(b - a) / a * 100.0 for a, b in zip(closes, closes[1:]) if a]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance)


def neutral_band_for(sigma_daily_pct: Optional[float], horizon_hours: float) -> Tuple[float, str]:
    """Banda neutra (in %) per un orizzonte, scalata sulla volatilita' misurata.

    banda = EQUIPROBABLE_Z * sigma_giornaliera * sqrt(giorni di orizzonte):
    cosi' "laterale" significa "movimento piu' piccolo di quello tipico" per
    QUELLA coppia su QUELL'orizzonte, non un numero uguale per XAUUSD a 3
    giorni e EURUSD a 8 ore. Ritorna (banda, metodo) per dichiararlo.
    """
    if sigma_daily_pct is None or sigma_daily_pct <= 0:
        return NEUTRAL_BAND_PCT, "fissa_fallback"
    days = max(1.0 / 24.0, horizon_hours / 24.0)
    band = EQUIPROBABLE_Z * sigma_daily_pct * math.sqrt(days)
    return max(NEUTRAL_BAND_MIN_PCT, band), "vol_scalata"


def classify_move(change_pct: float, band_pct: float = NEUTRAL_BAND_PCT) -> str:
    if change_pct > band_pct:
        return RIALZO
    if change_pct < -band_pct:
        return RIBASSO
    return LATERALE


def evaluate_due_scenarios(session: Session, limit: int = 500) -> Dict[str, int]:
    """Valuta gli scenari il cui orizzonte e' concluso e che non hanno esito."""
    now = now_utc()
    scenarios = list(
        session.execute(
            select(Scenario)
            .outerjoin(ScenarioOutcome)
            .where(Scenario.horizon_end <= now, ScenarioOutcome.id.is_(None))
            .order_by(Scenario.horizon_end.asc())
            .limit(limit)
        )
        .scalars()
        .all()
    )

    evaluated = 0
    skipped = 0
    # La volatilita' cambia lentamente: una stima al giorno per coppia basta,
    # e la cache evita 60 letture di barre per ognuno dei (potenzialmente
    # ventimila) scenari di una rigiocata lunga.
    sigma_cache: Dict[Any, Optional[float]] = {}
    for scenario in scenarios:
        start = price_at(session, scenario.pair, scenario.generated_at)
        # La fine puo' cadere a mercato chiuso: si accetta la prima chiusura
        # successiva (weekend/festivi), vedi price_at_or_next.
        end = price_at_or_next(session, scenario.pair, scenario.horizon_end)
        if start is None or end is None or start == 0:
            skipped += 1
            continue

        chiave = (scenario.pair, scenario.generated_at.date())
        if chiave not in sigma_cache:
            sigma_cache[chiave] = trailing_sigma_pct(
                session, scenario.pair, scenario.generated_at
            )
        horizon_hours = max(
            1.0, (scenario.horizon_end - scenario.generated_at).total_seconds() / 3600.0
        )
        band, _metodo = neutral_band_for(sigma_cache[chiave], horizon_hours)

        change_pct = (end - start) / start * 100.0
        realized = classify_move(change_pct, band)
        hit = realized == scenario.base_direction
        # Brier score binario sullo scenario base: (p - esito)^2.
        brier = (scenario.base_probability - (1.0 if hit else 0.0)) ** 2

        session.add(
            ScenarioOutcome(
                scenario_id=scenario.id,
                evaluated_at=now,
                price_start=start,
                price_end=end,
                change_pct=change_pct,
                realized_direction=realized,
                base_hit=hit,
                brier=brier,
                neutral_band_pct=band,
            )
        )
        evaluated += 1

    return {"valutati": evaluated, "senza_prezzi": skipped, "candidati": len(scenarios)}


# --------------------------------------------------------------------------- #
#  Metriche
# --------------------------------------------------------------------------- #

_BUCKETS = [(0.30, 0.40), (0.40, 0.50), (0.50, 0.65)]

# Fasce di |bias| per il breakdown: attorno alle soglie in gioco (direzione 8,
# etichetta 15) con risoluzione sufficiente a scegliere una soglia migliore.
_BIAS_BINS = [(0, 4), (4, 8), (8, 12), (12, 16), (16, 24), (24, 100)]


def reliability_metrics(session: Session, pair: Optional[str] = None) -> Dict[str, Any]:
    """Hit-rate, Brier score e curva di calibrazione semplificata."""
    stmt = select(Scenario, ScenarioOutcome).join(
        ScenarioOutcome, ScenarioOutcome.scenario_id == Scenario.id
    )
    if pair:
        stmt = stmt.where(Scenario.pair == pair.upper())
    rows = list(session.execute(stmt).all())

    total = len(rows)
    if total == 0:
        return {
            "campione": 0,
            "campione_sufficiente": False,
            "avviso": "Campione insufficiente: nessuno scenario ancora valutato.",
            "hit_rate": None,
            "brier": None,
            "calibrazione": [],
            "per_confidenza": {},
        }

    hits = sum(1 for _, outcome in rows if outcome.base_hit)
    brier = sum(outcome.brier for _, outcome in rows) / total

    # Distribuzione previsto/realizzato: senza questa tabella un hit-rate basso
    # e' indecifrabile. Se il previsto e' quasi sempre LATERALE e il realizzato
    # quasi mai, il problema e' la mappa bias->direzione (o la banda neutra),
    # non l'informazione macro.
    from collections import Counter
    previste = Counter(sc.base_direction for sc, _ in rows)
    realizzate = Counter(o.realized_direction for _, o in rows)
    hit_per_prevista: Dict[str, Any] = {}
    for direzione in sorted(previste):
        gruppo = [(sc, o) for sc, o in rows if sc.base_direction == direzione]
        centrati = sum(1 for _, o in gruppo if o.base_hit)
        hit_per_prevista[direzione] = {
            "campione": len(gruppo),
            "hit_rate": round(centrati / len(gruppo) * 100, 1),
        }

    calibration: List[Dict[str, Any]] = []
    for low, high in _BUCKETS:
        bucket = [
            (s, o) for s, o in rows if low <= s.base_probability < high
        ]
        if not bucket:
            calibration.append(
                {
                    "bucket": f"{int(low * 100)}-{int(high * 100)}%",
                    "campione": 0,
                    "probabilita_media": None,
                    "frequenza_reale": None,
                    "scarto": None,
                }
            )
            continue
        avg_prob = sum(s.base_probability for s, _ in bucket) / len(bucket)
        realized = sum(1 for _, o in bucket if o.base_hit) / len(bucket)
        calibration.append(
            {
                "bucket": f"{int(low * 100)}-{int(high * 100)}%",
                "campione": len(bucket),
                "probabilita_media": round(avg_prob * 100, 1),
                "frequenza_reale": round(realized * 100, 1),
                "scarto": round((realized - avg_prob) * 100, 1),
            }
        )

    by_confidence: Dict[str, Any] = {}
    for level in ("ALTA", "MEDIA", "BASSA"):
        subset = [(s, o) for s, o in rows if s.confidence == level]
        if not subset:
            continue
        by_confidence[level] = {
            "campione": len(subset),
            "hit_rate": round(
                sum(1 for _, o in subset if o.base_hit) / len(subset) * 100, 1
            ),
            "brier": round(sum(o.brier for _, o in subset) / len(subset), 3),
        }

    # Confidenza separata per classe prevista. Da quando ogni LATERALE senza
    # segnale e' BASSA per regola, la tabella marginale qui sopra confronta
    # classi diverse, non livelli di confidenza: BASSA si riempie di laterali
    # (misurato: 89% del campione, hit 41,9%) e ALTA di direzionali (hit piu'
    # basso), quindi la scala sembra invertita anche quando non lo e'. Il
    # giudizio onesto si fa a parita' di classe prevista.
    def _confidence_split(direzionale: bool) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for level in ("ALTA", "MEDIA", "BASSA"):
            subset = [
                (s, o)
                for s, o in rows
                if s.confidence == level
                and (s.base_direction != LATERALE) == direzionale
            ]
            if not subset:
                continue
            out[level] = {
                "campione": len(subset),
                "hit_rate": round(
                    sum(1 for _, o in subset if o.base_hit) / len(subset) * 100, 1
                ),
                "brier": round(sum(o.brier for _, o in subset) / len(subset), 3),
            }
        return out

    per_confidenza_direzionale = _confidence_split(direzionale=True)
    per_confidenza_laterale = _confidence_split(direzionale=False)

    # Hit per fascia di |bias|: lo strumento con cui SCEGLIERE la soglia di
    # direzione invece di indovinarla. Misurato al primo giro: soglia 15
    # dava hit direzionale 37-42% su pochissime chiamate, soglia 8 lo ha
    # diluito a 26-29% (pari alla frequenza di base del mercato). La soglia
    # giusta e' quella sopra cui l'hit direzionale batte la frequenza di
    # base realizzata — e si legge da questa tabella, non si decide a occhio.
    per_bias: List[Dict[str, Any]] = []
    for low, high in _BIAS_BINS:
        gruppo = [(s, o) for s, o in rows if low <= abs(s.bias) < high]
        etichetta = f"{low}-{high}" if high < 100 else f"{low}+"
        if not gruppo:
            per_bias.append(
                {
                    "fascia_bias": etichetta,
                    "campione": 0,
                    "hit_rate": None,
                    "direzionali": 0,
                    "hit_rate_direzionale": None,
                }
            )
            continue
        direzionali = [(s, o) for s, o in gruppo if s.base_direction != LATERALE]
        per_bias.append(
            {
                "fascia_bias": etichetta,
                "campione": len(gruppo),
                "hit_rate": round(
                    sum(1 for _, o in gruppo if o.base_hit) / len(gruppo) * 100, 1
                ),
                "direzionali": len(direzionali),
                "hit_rate_direzionale": round(
                    sum(1 for _, o in direzionali if o.base_hit) / len(direzionali) * 100, 1
                )
                if direzionali
                else None,
            }
        )

    # La banda neutra e' adattiva: qui si dichiara quella effettivamente usata
    # sugli esiti del campione. Gli esiti valutati prima dell'introduzione
    # (colonna NULL) usavano la banda fissa e vanno rivalutati con una nuova
    # rigiocata per essere confrontabili.
    bande = [o.neutral_band_pct for _, o in rows if o.neutral_band_pct is not None]
    banda_neutra = {
        "metodo": (
            f"adattiva: ±{EQUIPROBABLE_Z} × σ giornaliera × √(giorni di orizzonte), "
            f"pavimento ±{NEUTRAL_BAND_MIN_PCT}%, riserva fissa ±{NEUTRAL_BAND_PCT}% "
            f"sotto {VOL_MIN_BARS} barre di storico prezzi"
        ),
        "media_pct": round(sum(bande) / len(bande), 3) if bande else None,
        "min_pct": round(min(bande), 3) if bande else None,
        "max_pct": round(max(bande), 3) if bande else None,
        "esiti_valutati_con_banda_fissa_storica": total - len(bande),
    }

    sufficient = total >= MIN_SAMPLE
    return {
        "campione": total,
        "banda_neutra": banda_neutra,
        "direzioni": {
            "previste": dict(previste),
            "realizzate": dict(realizzate),
            "hit_per_direzione_prevista": hit_per_prevista,
        },
        "campione_sufficiente": sufficient,
        "avviso": None
        if sufficient
        else (
            f"Campione insufficiente ({total}/{MIN_SAMPLE} scenari valutati): "
            "le metriche non sono ancora informative."
        ),
        "hit_rate": round(hits / total * 100, 1),
        "brier": round(brier, 3),
        "brier_riferimento_casuale": 0.25,
        "calibrazione": calibration,
        "per_confidenza": by_confidence,
        "per_confidenza_direzionale": per_confidenza_direzionale,
        "per_confidenza_laterale": per_confidenza_laterale,
        "nota_confidenza": (
            "La tabella per_confidenza marginale mescola le classi: i LATERALE "
            "senza segnale sono BASSA per regola, quindi i livelli non sono "
            "confrontabili fra loro. La scala si giudica sulle due tabelle "
            "separate (direzionale e laterale)."
        ),
        "per_bias": per_bias,
        "nota_bias": (
            "Hit per fascia di |bias| dello scenario: serve a scegliere la "
            "soglia di direzione dai dati. Confronta hit_rate_direzionale con "
            "la frequenza di base della direzione realizzata (tabella "
            "direzioni): sotto la fascia in cui la batte, meglio LATERALE."
        ),
        "nota_metodo": (
            "Brier score binario sullo scenario base: (probabilita' − esito)^2. "
            "Esito direzionale calcolato sulla variazione percentuale della coppia "
            "fra generazione e fine orizzonte, con banda neutra ADATTIVA scalata "
            "su volatilita' della coppia e lunghezza dell'orizzonte (sotto un "
            "random walk i tre esiti sono equiprobabili, ~33% ciascuno). "
            "Volatilita' stimata solo su barre precedenti alla generazione: "
            "nessun lookahead nella soglia."
        ),
    }
