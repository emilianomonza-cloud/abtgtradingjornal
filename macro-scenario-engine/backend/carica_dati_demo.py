#!/usr/bin/env python3
"""Carica dati DIMOSTRATIVI nel database, per vedere la dashboard popolata.

I dati sono INVENTATI a scopo di dimostrazione: eventi di calendario, comunicati
delle banche centrali, stato delle fonti e uno storico di scenari gia' valutati.
Servono a mostrare come si comporta il sistema, non a operare.

Uso (dalla cartella backend, con l'ambiente virtuale attivo):
    python carica_dati_demo.py

Per tornare a un database pulito basta cancellare `data/macro_scenario.db`.
"""

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.storage.database import init_db, session_scope
from app.storage.repositories import now_utc, upsert_event, record_source_health
from app.engine import surprise, taxonomy
from app.enrichment.service import upsert_document
from app.calibration import evaluation as calib
from app.storage.models import Event, Scenario
from app.pipeline import refresh

def ev(s, ccy, title, hours, impact, actual=None, forecast=None, previous=None, raw=None):
    e, _ = upsert_event(s, {
        "timestamp_utc": now_utc() + timedelta(hours=hours),
        "currency": ccy, "title": title,
        "category": taxonomy.classify_category(title), "impact": impact,
        "actual": actual, "forecast": forecast, "previous": previous,
        "actual_raw": (raw[0] if raw else None), "forecast_raw": (raw[1] if raw else None),
        "previous_raw": (raw[2] if raw else None), "source": "forexfactory_json",
    })
    s.flush()
    if e.actual is not None:
        surprise.apply_and_store(s, e)
    return e

init_db(seed=True)
with session_scope() as s:
    s.query(Event).delete(); s.query(Scenario).delete(); s.flush()

    # --- storico per stimare le sigma
    for i, (a, f, p) in enumerate([(236,190,210),(175,180,236),(199,185,175),(150,190,199),
                                   (227,200,150),(165,180,227),(254,195,165),(142,175,254)]):
        ev(s, "USD", "Non-Farm Employment Change", -24*30*(i+1), "RED", a, f, p,
           (f"{a}K", f"{f}K", f"{p}K"))
    for i, (a, f, p) in enumerate([(3.3,3.2,3.4),(3.2,3.3,3.3),(3.4,3.3,3.2),
                                   (3.5,3.4,3.4),(3.3,3.4,3.5),(3.6,3.5,3.3)]):
        ev(s, "USD", "Core CPI y/y", -24*30*(i+1)-6, "RED", a, f, p,
           (f"{a}%", f"{f}%", f"{p}%"))

    # --- eventi recenti gia' rilasciati
    ev(s, "USD", "Non-Farm Employment Change", -30, "RED", 305, 180, 175, ("305K","180K","175K"))
    ev(s, "USD", "Core CPI y/y", -52, "RED", 3.4, 3.1, 3.2, ("3.4%","3.1%","3.2%"))
    ev(s, "USD", "ISM Services PMI", -74, "ORANGE", 54.2, 52.8, 52.5)
    ev(s, "EUR", "Core CPI y/y", -46, "RED", 2.1, 2.3, 2.4, ("2.1%","2.3%","2.4%"))
    ev(s, "EUR", "Manufacturing PMI", -70, "ORANGE", 46.8, 48.1, 48.4)
    ev(s, "EUR", "German ZEW Economic Sentiment", -96, "ORANGE", 18.4, 24.0, 25.6)
    ev(s, "GBP", "Average Earnings Index 3m/y", -55, "RED", 5.6, 5.1, 5.2, ("5.6%","5.1%","5.2%"))
    ev(s, "GBP", "Retail Sales m/m", -80, "ORANGE", -0.6, 0.2, 0.4, ("-0.6%","0.2%","0.4%"))
    ev(s, "JPY", "National Core CPI y/y", -60, "RED", 2.9, 2.7, 2.6, ("2.9%","2.7%","2.6%"))
    ev(s, "AUD", "Employment Change", -44, "RED", 12.1, 25.0, 31.2, ("12.1K","25.0K","31.2K"))
    ev(s, "CAD", "Trade Balance", -90, "ORANGE", -1.2, 0.4, 0.1, ("-1.2B","0.4B","0.1B"))
    ev(s, "USD", "Unemployment Rate", -30, "RED", 4.0, 4.2, 4.2, ("4.0%","4.2%","4.2%"))

    # --- eventi futuri (countdown, no-trade, scenari riscrivibili)
    ev(s, "USD", "CPI m/m", 3.5, "RED", None, 0.3, 0.2, (None,"0.3%","0.2%"))
    ev(s, "USD", "CPI y/y", 3.5, "RED", None, 3.2, 3.4, (None,"3.2%","3.4%"))
    ev(s, "EUR", "ECB Press Conference", 21, "RED")
    ev(s, "EUR", "Main Refinancing Rate", 20.5, "RED", None, 2.00, 2.00)
    ev(s, "GBP", "Official Bank Rate", 44, "RED", None, 4.00, 4.00)
    ev(s, "USD", "Federal Funds Rate", 68, "RED", None, 4.00, 4.00)
    ev(s, "USD", "FOMC Press Conference", 68.5, "RED")
    ev(s, "AUD", "Cash Rate", 92, "RED", None, 3.60, 3.60)
    ev(s, "USD", "Non-Farm Employment Change", 118, "RED", None, 165, 305, (None,"165K","305K"))
    ev(s, "JPY", "BOJ Policy Rate", 140, "RED", None, 0.50, 0.50)
    ev(s, "EUR", "German Ifo Business Climate", 26, "ORANGE", None, 87.5, 86.9)
    ev(s, "CAD", "Overnight Rate", 150, "RED", None, 2.25, 2.25)

    # --- documenti banche centrali analizzati dal layer semantico
    docs = [
        ("FED","USD","PRESS_RELEASE","FOMC statement: Committee maintains the target range",
         "Inflation remains elevated and the labour market is robust. The Committee judges that "
         "further tightening may be appropriate to return inflation to 2 percent over time. "
         "Upside risks to inflation persist and the Committee remains vigilant.", -30),
        ("FED","USD","SPEECH","Speech by Governor on the economic outlook",
         "Wage growth remains strong and price pressures persist in services. It is premature to "
         "declare victory on inflation; policy should stay sufficiently restrictive.", -110),
        ("BCE","EUR","PRESS_RELEASE","Monetary policy decisions",
         "Disinflation is proceeding and inflation is moving toward target. The Governing Council "
         "notes a softening labour market and downside risks to growth; a rate cut remains on the "
         "table should the disinflationary process continue.", -50),
        ("BCE","EUR","MINUTES","Account of the monetary policy meeting",
         "Members observed weakening demand and an economic slowdown in manufacturing. Several "
         "members favoured an accommodative stance.", -200),
        ("BOE","GBP","PRESS_RELEASE","Bank Rate maintained: monetary policy summary",
         "Second-round effects and strong wage growth keep domestic price pressures elevated. The "
         "Committee remains vigilant and policy will stay restrictive for sufficiently long.", -70),
        ("BOJ","JPY","PRESS_RELEASE","Statement on Monetary Policy",
         "The Bank will continue asset purchases and maintain an accommodative stance while "
         "monitoring price developments.", -150),
        ("RBA","AUD","PRESS_RELEASE","Monetary Policy Decision",
         "The Board noted a labour market cooling and slowing economy; disinflation is under way.", -90),
        ("BOC","CAD","PRESS_RELEASE","Bank of Canada maintains policy rate",
         "Downside risks to growth have increased. The Governing Council is prepared to lower rates "
         "further if disinflation continues.", -130),
    ]
    for bank, ccy, dtype, title, body, hours in docs:
        upsert_document(s, {"url": f"https://example.org/{bank}/{abs(hours)}", "bank": bank,
                            "currency": ccy, "doc_type": dtype, "title": title,
                            "published_at": now_utc() + timedelta(hours=hours),
                            "body": body, "source": f"{bank.lower()}_press"})

    # --- stato delle fonti (una degradata, per mostrare il comportamento onesto)
    # Stato fonti: due volutamente non OK, per mostrare come il sistema
    # dichiara i problemi invece di nasconderli.
    for name, label, status, items, msg, lat in [
        ("forexfactory_json","Forex Factory — feed JSON settimanale","OK",24,"Aggiornamento completato.",342.0),
        ("csv_manuale","Import manuale CSV / API","OK",0,"Fonte passiva: import manuale via API.",None),
        ("fed_press","Federal Reserve — comunicati","OK",2,"2 nuovi documenti analizzati.",188.0),
        ("fed_speeches","Federal Reserve — discorsi","OK",1,"1 nuovi documenti analizzati.",204.0),
        ("ecb_press","BCE — comunicati stampa","OK",2,"2 nuovi documenti analizzati.",231.0),
        ("boe_news","Bank of England — news","OK",1,"1 nuovi documenti analizzati.",412.0),
        ("boj_news","Bank of Japan — what's new","OK",1,"1 nuovi documenti analizzati.",520.0),
        ("rba_releases","RBA — media releases","OK",1,"1 nuovi documenti analizzati.",287.0),
        ("boc_releases","Bank of Canada — press releases","OK",1,"1 nuovi documenti analizzati.",265.0),
        ("snb_releases","SNB — comunicati stampa","DEGRADED",0,"Feed raggiunto ma nessun documento riconosciuto.",900.0),
        ("rbnz_news","RBNZ — news","DOWN",0,"Fonte non raggiungibile: timeout dopo 3 tentativi.",None),
    ]:
        record_source_health(s, name, status, label=label, items=items, message=msg, latency_ms=lat)

    s.flush()
    refresh(s)

# --- storico scenari valutati, per la pagina Affidabilita'
with session_scope() as s:
    import random
    random.seed(7)
    pairs = ["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","EURGBP","XAUUSD"]
    price0 = {"EURUSD":1.09,"GBPUSD":1.27,"USDJPY":152.0,"AUDUSD":0.65,
              "USDCAD":1.37,"EURGBP":0.855,"XAUUSD":2400.0}
    rows = []
    for i in range(42):
        pair = pairs[i % len(pairs)]
        gen = now_utc() - timedelta(days=60) + timedelta(hours=i*24)
        end = gen + timedelta(hours=72)
        direction = random.choice(["RIALZO","RIBASSO","LATERALE"])
        prob = round(random.uniform(0.40, 0.63), 3)
        conf = "ALTA" if prob > 0.57 else ("MEDIA" if prob > 0.47 else "BASSA")
        s.add(Scenario(pair=pair, horizon="h24_72", generated_at=gen,
                       expires_at=gen+timedelta(hours=12), horizon_end=end,
                       bias=round(random.uniform(-45,45),1), bias_label="NEUTRAL",
                       confidence=conf, confidence_score=prob*100,
                       base_direction=direction, base_probability=prob,
                       alt_a_probability=round((1-prob)*0.55,3),
                       alt_b_probability=round((1-prob)*0.45,3), payload={}))
        # prezzo coerente: lo scenario "azzecca" con frequenza vicina alla sua probabilita'
        hit = random.random() < prob
        move = {"RIALZO":1,"RIBASSO":-1,"LATERALE":0}[direction] * (1 if hit else -1)
        if direction == "LATERALE":
            change = random.uniform(-0.1,0.1) if hit else random.uniform(0.4,1.0)*random.choice([-1,1])
        else:
            change = move * random.uniform(0.35, 1.4)
        p0 = price0[pair]
        rows.append((pair, gen, p0))
        rows.append((pair, end, p0*(1+change/100)))
    csv = "pair,timestamp,close\n" + "\n".join(
        f"{p},{t.strftime('%Y-%m-%d %H:%M')},{c:.5f}" for p, t, c in rows)
    calib.import_prices_csv(s, csv)
    s.flush()
    esito = calib.evaluate_due_scenarios(s)
    metriche = calib.reliability_metrics(s)
    print()
    print("Dati dimostrativi caricati.")
    print(f"  scenari storici valutati : {esito['valutati']}")
    print(f"  hit-rate                 : {metriche['hit_rate']}%")
    print(f"  Brier score              : {metriche['brier']} (riferimento casuale: 0.25)")
    print()
    print("Avvia il server e apri http://127.0.0.1:8000")
    print("  uvicorn app.main:app --host 127.0.0.1 --port 8000")
