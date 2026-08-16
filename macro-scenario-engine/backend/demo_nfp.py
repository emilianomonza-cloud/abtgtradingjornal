#!/usr/bin/env python3
"""FASE 9 — Test end-to-end: simulazione di un NFP BIG_BEAT sul dollaro.

Mostra, in ordine:
  1. l'evento normalizzato come lo vede il database;
  2. il surprise score calcolato (con la sigma stimata dallo storico);
  3. lo score USD prima e dopo, sotto-score per sotto-score;
  4. lo scenario EURUSD prima e dopo (probabilita', confidenza, invalidazioni);
  5. il payload di /api/v1/mt5/snapshot.

Uso:
    python demo_nfp.py            # database temporaneo, non tocca i tuoi dati
    python demo_nfp.py --keep-db  # usa il database configurato in .env
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _use_temp_database() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mse-demo-"))
    os.environ["MSE_DB_PATH"] = str(tmp / "demo.db")
    os.environ["MSE_CACHE_DIR"] = str(tmp / "cache")
    os.environ["MSE_MT5_FILES_DIR"] = str(tmp / "mt5")
    os.environ["MSE_NETWORK_ENABLED"] = "false"
    os.environ["MSE_SCHEDULER_ENABLED"] = "false"


parser = argparse.ArgumentParser(description="Simulazione NFP BIG_BEAT end-to-end")
parser.add_argument("--keep-db", action="store_true", help="usa il database di .env")
ARGS = parser.parse_args()
if not ARGS.keep_db:
    _use_temp_database()

from app.engine import scenarios as scenario_engine  # noqa: E402
from app.engine import scoring, surprise  # noqa: E402
from app.pipeline import build_mt5_snapshot  # noqa: E402
from app.storage.database import init_db, session_scope  # noqa: E402
from app.storage.models import Event  # noqa: E402
from app.storage.repositories import now_utc, upsert_event  # noqa: E402

LINE = "=" * 78


def title(text: str) -> None:
    print(f"\n{LINE}\n {text}\n{LINE}")


def add_event(session, *, currency, event_title, days_ago, actual, forecast, previous, impact="RED"):
    from app.engine.taxonomy import classify_category

    stamp = now_utc() - timedelta(days=days_ago)
    event, _ = upsert_event(
        session,
        {
            "timestamp_utc": stamp,
            "currency": currency,
            "title": event_title,
            "category": classify_category(event_title),
            "impact": impact,
            "actual": actual,
            "forecast": forecast,
            "previous": previous,
            "actual_raw": f"{actual:g}K" if actual is not None and "Employment" in event_title else None,
            "forecast_raw": f"{forecast:g}K" if forecast is not None and "Employment" in event_title else None,
            "source": "demo",
        },
    )
    session.flush()
    if event.actual is not None:
        surprise.apply_and_store(session, event)
    return event


def print_scores(evaluation) -> None:
    print(f"  score composito : {evaluation.composite:+.1f}")
    print(f"  qualita' dati   : {evaluation.data_quality:.0%}   regime: {evaluation.regime}")
    for name, sub in evaluation.subscores.items():
        print(f"    {scoring.SUBSCORE_LABELS[name]:<22} {sub.value:+7.1f}  q={sub.quality:.2f}  {sub.rationale[:78]}")


def print_scenarios(payload) -> None:
    print(f"  bias {payload['bias']:+.1f} ({payload['bias_label']})   "
          f"confidenza {payload['confidenza']}   "
          f"riscrivibile: {'si' if payload['riscrivibile'] else 'no'}")
    for scenario in payload["scenari"]:
        print(f"    [{scenario['tipo']:<5}] {scenario['direzione']:<8} "
              f"{scenario['probabilita']:5.1f}%  magnitudo {scenario['magnitudo']:<9} "
              f"conf. {scenario['confidenza']}")
        print(f"            suggerimento: {scenario['suggerimento']['azione']}")
    print("  catene causali attive nello scenario base:")
    for chain in payload["scenari"][0]["catene_causali"][:4]:
        print(f"    - {chain['nome']} [{chain['valuta']}] -> {chain['effetto_valuta']}"
              f" (intensita {chain['intensita']})")
    print("  condizioni di invalidazione dello scenario base:")
    for condition in payload["scenari"][0]["invalidazione"]:
        print(f"    - {condition}")


def main() -> int:
    init_db(seed=True)

    with session_scope() as session:
        session.query(Event).delete()
        session.flush()

        title("SETUP — storico usato per stimare la deviazione tipica dell'indicatore")
        history = [
            (35, 236.0, 190.0, 210.0),
            (65, 175.0, 180.0, 236.0),
            (95, 199.0, 185.0, 175.0),
            (125, 150.0, 190.0, 199.0),
            (155, 227.0, 200.0, 150.0),
            (185, 165.0, 180.0, 227.0),
            (215, 254.0, 195.0, 165.0),
            (245, 142.0, 175.0, 254.0),
        ]
        for days, actual, forecast, previous in history:
            add_event(
                session,
                currency="USD",
                event_title="Non-Farm Employment Change",
                days_ago=days,
                actual=actual,
                forecast=forecast,
                previous=previous,
            )
        print(f"  {len(history)} rilasci NFP storici inseriti (actual vs forecast)")

        # Contesto minimo sulle due valute della coppia.
        add_event(session, currency="USD", event_title="Core CPI y/y", days_ago=12,
                  actual=3.1, forecast=3.1, previous=3.2, impact="RED")
        add_event(session, currency="EUR", event_title="Core CPI y/y", days_ago=10,
                  actual=2.2, forecast=2.3, previous=2.4, impact="RED")
        add_event(session, currency="EUR", event_title="Manufacturing PMI", days_ago=8,
                  actual=47.5, forecast=48.0, previous=48.2, impact="ORANGE")
        print("  contesto: CPI core USA/EUR e PMI manifatturiero area euro")

        sigma, sigma_source = surprise.estimate_sigma(session, "USD", "NFP")
        print(f"  sigma stimata per NFP: {sigma:.1f}K  (fonte: {sigma_source})")

        # ------------------------------------------------------------------ #
        title("PRIMA DEL RILASCIO")
        before_scores = scoring.evaluate_all(session)
        print(" USD")
        print_scores(before_scores["USD"])
        print(" EUR")
        print_scores(before_scores["EUR"])
        before_scenarios = scenario_engine.generate_pair_scenarios(
            session, "EURUSD", before_scores
        )
        print("\n EURUSD")
        print_scenarios(before_scenarios)

        # ------------------------------------------------------------------ #
        title("RILASCIO — NFP USA: actual 350K vs forecast 180K (BIG_BEAT)")
        nfp = add_event(
            session,
            currency="USD",
            event_title="Non-Farm Employment Change",
            days_ago=0.02,  # ~30 minuti fa
            actual=350.0,
            forecast=180.0,
            previous=175.0,
        )
        print("\n 1) EVENTO NORMALIZZATO")
        for key, value in nfp.to_dict().items():
            print(f"    {key:<16}: {value}")

        print("\n 2) SURPRISE SCORE")
        result = surprise.compute_surprise(session, nfp)
        for key, value in result.to_dict().items():
            print(f"    {key:<16}: {value}")

        from app.engine.playbooks import expected_reaction, exceptions_for, second_order_for

        print("\n    reazione attesa da playbook (regime HAWKISH_DOMINANT):")
        print(f"    {expected_reaction('NFP', 'USD', result.label, 'HAWKISH_DOMINANT')}")
        print("    eccezioni note:")
        for exception in exceptions_for("NFP", "USD"):
            print(f"      - {exception}")
        print("    effetti di secondo ordine:")
        for effect in second_order_for("NFP", "USD"):
            print(f"      - {effect}")

        # ------------------------------------------------------------------ #
        title("DOPO IL RILASCIO")
        after_scores = scoring.evaluate_all(session)
        print(" USD")
        print_scores(after_scores["USD"])

        delta = after_scores["USD"].composite - before_scores["USD"].composite
        print(f"\n 3) AGGIORNAMENTO SCORE USD: "
              f"{before_scores['USD'].composite:+.1f} -> {after_scores['USD'].composite:+.1f} "
              f"({delta:+.1f})")

        after_scenarios = scenario_engine.generate_pair_scenarios(
            session, "EURUSD", after_scores
        )
        print("\n 4) SCENARIO EURUSD PRIMA / DOPO")
        print("  PRIMA:")
        print_scenarios(before_scenarios)
        print("\n  DOPO:")
        print_scenarios(after_scenarios)

        # ------------------------------------------------------------------ #
        title("5) PAYLOAD /api/v1/mt5/snapshot")
        generated = scenario_engine.generate_all(session, after_scores, persist=False)
        snapshot = build_mt5_snapshot(session, after_scores, generated)
        print(json.dumps(snapshot, ensure_ascii=False, indent=1)[:4000])

        title("ESITO")
        checks = [
            ("evento classificato come NFP", nfp.category == "NFP"),
            ("sorpresa classificata BIG_BEAT", result.label == "BIG_BEAT"),
            ("impatto positivo sul dollaro", result.impact > 0),
            ("score USD in aumento", delta > 0),
            ("EURUSD piu' ribassista", after_scenarios["bias"] < before_scenarios["bias"]),
            ("probabilita' sommano a 100", abs(sum(s["probabilita"] for s in after_scenarios["scenari"]) - 100) < 0.2),
            ("cap 65% rispettato", after_scenarios["scenari"][0]["probabilita"] <= 65.05),
            ("snapshot con almeno una coppia", len(snapshot["pairs"]) > 0),
        ]
        ok = True
        for label, passed in checks:
            print(f"  [{'OK ' if passed else 'KO '}] {label}")
            ok = ok and passed
        print()
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
