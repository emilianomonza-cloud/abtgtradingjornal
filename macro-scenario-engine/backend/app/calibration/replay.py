"""Rigiocata storica: trasforma lo storico importato in scenari valutabili.

PERCHE' SERVE
-------------
La pagina "Affidabilita'" resta vuota finche' non esistono scenari *scaduti* e
confrontati con i prezzi reali. Aspettando il tempo reale servono settimane.
Con uno storico di calendario importato (`collectors/ff_range.py`) e uno storico
prezzi importato da MT5, lo stesso risultato si ottiene rigiocando il passato.

COME FUNZIONA
-------------
Per ogni istante della griglia temporale si congela l'orologio del sistema
(`repositories.clock_frozen_at`) e si esegue la pipeline normale: score, bias,
scenari. Congelare l'orologio invece di aggiungere un parametro "data" a ogni
funzione non e' una scorciatoia: e' la garanzia che nessun ramo del motore possa
leggere dati successivi alla data simulata, comprese le query che filtrano su
`now_utc()` in profondita'.

Gli scenari prodotti finiscono nella stessa tabella di quelli reali, marcati con
`origine: rigiocata_storica`, e vengono poi valutati dal modulo di calibrazione
esattamente come gli altri.

LIMITI DICHIARATI — questi numeri NON sono un backtest di una strategia
----------------------------------------------------------------------
1. **Tassi di policy ricostruiti.** Vengono dedotti dalle decisioni di tasso
   presenti nel calendario storico. Dove il calendario non copre una banca
   centrale, quella valuta resta senza tasso e il sotto-score differenziale
   entra come neutro, con qualita' zero.
2. **Nessun comunicato storico.** L'archivio dei documenti delle banche centrali
   si riempie solo in avanti: nel passato il sotto-score `cb_stance` (15%) e'
   quasi sempre neutro. Gli scenari rigiocati sono quindi piu' poveri di quelli
   generati in tempo reale.
3. **Revisioni.** Il calendario riporta i valori come sono *oggi*, comprese le
   revisioni pubblicate dopo. Chi guardava lo schermo quel giorno vedeva la
   prima stampa. E' una forma di lookahead che non si puo' eliminare a partire
   da questa fonte, e va tenuta presente leggendo l'hit-rate.
4. **Nessun costo di esecuzione.** Spread, slippage e commissioni non entrano:
   si misura la direzione del prezzo, non il risultato di un conto reale.

Tutti e quattro i limiti vengono restituiti nel report e mostrati in dashboard:
un hit-rate ottenuto cosi' e' un indizio sulla qualita' del modello, non una
promessa di rendimento.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_config
from ..engine import scenarios as scenario_engine
from ..engine import scoring
from ..storage.models import Event, PolicyRate, Scenario
from ..storage.repositories import clock_frozen_at, naive_utc, now_utc
from . import evaluation

logger = logging.getLogger(__name__)

ORIGINE = "rigiocata_storica"

# Tetto al numero di passi: una richiesta sbagliata (dieci anni a passo orario)
# non deve poter bloccare il processo.
MAX_PASSI = 2000

LIMITI = [
    "Tassi di policy ricostruiti dalle decisioni presenti nel calendario storico: "
    "dove mancano, il sotto-score differenziale entra come neutro.",
    "Nessun comunicato di banca centrale nel passato: il sotto-score stance e' "
    "quasi sempre neutro, quindi gli scenari rigiocati sono piu' poveri di quelli reali.",
    "Il calendario riporta i valori revisionati, non la prima stampa: e' una forma "
    "di lookahead non eliminabile da questa fonte.",
    "Nessun costo di esecuzione: si misura la direzione del prezzo, non il risultato "
    "di un conto reale.",
]


# --------------------------------------------------------------------------- #
#  Tassi di policy storici
# --------------------------------------------------------------------------- #


def rates_asof(session: Session, moment: datetime) -> Dict[str, PolicyRate]:
    """Ricostruisce i tassi di policy alla data indicata.

    Fonte: l'ultima decisione di tasso con valore effettivo pubblicata entro
    `moment`. Gli oggetti restituiti NON vengono salvati: servono solo al
    calcolo e non toccano i tassi correnti in archivio.
    """
    config = get_config()
    moment = naive_utc(moment)

    # I nomi delle banche centrali si prendono dai tassi gia' in archivio: e' una
    # semplice etichetta, non entra nel calcolo.
    nomi = {
        r.currency: r.central_bank
        for r in session.execute(select(PolicyRate)).scalars().all()
    }

    out: Dict[str, PolicyRate] = {}
    for currency in config.currencies:
        decisione = session.execute(
            select(Event)
            .where(
                Event.currency == currency,
                Event.category == "RATE_DECISION",
                Event.actual.is_not(None),
                Event.timestamp_utc <= moment,
            )
            .order_by(Event.timestamp_utc.desc())
            .limit(1)
        ).scalars().first()
        if decisione is None:
            continue
        out[currency] = PolicyRate(
            currency=currency,
            central_bank=nomi.get(currency, "n/d"),
            rate=float(decisione.actual),
            effective_date=decisione.timestamp_utc,
            updated_at=decisione.timestamp_utc,
            source="ricostruito_da_calendario",
        )
    return out


# --------------------------------------------------------------------------- #
#  Griglia temporale
# --------------------------------------------------------------------------- #


def _passi(
    start: datetime, end: datetime, step_hours: int, solo_giorni_feriali: bool
) -> List[datetime]:
    """Istanti da simulare, dal piu' vecchio al piu' recente."""
    passi: List[datetime] = []
    corrente = naive_utc(start)
    limite = naive_utc(end)
    delta = timedelta(hours=max(1, step_hours))
    while corrente <= limite and len(passi) < MAX_PASSI:
        # Il fine settimana il mercato FX e' chiuso: uno scenario generato di
        # domenica misurerebbe un movimento inesistente.
        if not (solo_giorni_feriali and corrente.weekday() >= 5):
            passi.append(corrente)
        corrente += delta
    return passi


# --------------------------------------------------------------------------- #
#  Rigiocata
# --------------------------------------------------------------------------- #


def replay(
    session: Session,
    start: datetime,
    end: datetime,
    step_hours: int = 24,
    horizon: Optional[str] = None,
    solo_giorni_feriali: bool = True,
    sostituisci: bool = True,
) -> Dict[str, Any]:
    """Genera scenari storici e li valuta contro i prezzi importati."""
    start = naive_utc(start)
    end = naive_utc(end)
    adesso = now_utc()

    if end > adesso:
        # Un orizzonte che finisce nel futuro non e' valutabile: non ha senso
        # generarlo e conteggiarlo come campione.
        end = adesso
    if start >= end:
        raise ValueError(
            "Intervallo non valido: la data di inizio deve precedere quella di fine, "
            "e la fine non puo' essere nel futuro."
        )

    eventi_nel_periodo = session.execute(
        select(Event).where(Event.timestamp_utc >= start, Event.timestamp_utc <= end).limit(1)
    ).scalars().first()
    if eventi_nel_periodo is None:
        raise ValueError(
            "Nessun evento in archivio nel periodo richiesto: importa prima lo storico "
            "del calendario. Senza eventi la rigiocata produrrebbe solo scenari neutri."
        )

    rimossi = _rimuovi_precedenti(session, start, end) if sostituisci else 0

    passi = _passi(start, end, step_hours, solo_giorni_feriali)
    # Il tetto MAX_PASSI protegge da richieste sbagliate, ma NON deve essere
    # silenzioso: se tronca, il report lo dichiara e dice come completare.
    troncato = len(passi) >= MAX_PASSI and bool(passi) and passi[-1] < end
    generati = 0
    falliti = 0

    for istante in passi:
        try:
            with clock_frozen_at(istante):
                rates = rates_asof(session, istante)
                evaluations = scoring.evaluate_all(session, rates)
                payloads = scenario_engine.generate_all(
                    session, evaluations, horizon, persist=False
                )
                for payload in payloads.values():
                    payload["origine"] = ORIGINE
                    payload["nota_origine"] = (
                        "Scenario ricostruito a posteriori con i soli dati disponibili "
                        "a quella data. Non e' uno scenario che il sistema abbia "
                        "davvero pubblicato quel giorno."
                    )
                    scenario_engine.persist_scenario(session, payload)
                    generati += 1
            session.flush()
        except Exception:  # noqa: BLE001 - un passo storto non ferma la rigiocata
            logger.exception("Passo di rigiocata fallito su %s", istante)
            falliti += 1

    session.flush()
    valutazione = evaluation.evaluate_due_scenarios(session, limit=max(500, generati + 100))

    avvisi: List[str] = []
    if troncato:
        avvisi.append(
            f"PERIODO TRONCATO: il tetto di {MAX_PASSI} passi ferma questa "
            f"rigiocata al {passi[-1]:%Y-%m-%d}, non al {end:%Y-%m-%d} richiesto. "
            f"Per completare, lancia una seconda rigiocata dal "
            f"{passi[-1] + timedelta(days=1):%Y-%m-%d} al {end:%Y-%m-%d}."
        )

    return {
        "periodo": [start.isoformat(), end.isoformat()],
        "periodo_effettivo": [passi[0].isoformat(), passi[-1].isoformat()] if passi else None,
        "troncato": troncato,
        "avvisi": avvisi,
        "passi": len(passi),
        "passi_falliti": falliti,
        "scenari_generati": generati,
        "scenari_rimossi": rimossi,
        "valutazione": valutazione,
        "tassi_ricostruiti": sorted(rates_asof(session, end).keys()),
        "limiti": LIMITI,
        "avviso": (
            "Numeri ottenuti rigiocando il passato: indicano la qualita' del modello, "
            "non un rendimento ottenibile. Leggili insieme ai limiti dichiarati."
        ),
    }


def _rimuovi_precedenti(session: Session, start: datetime, end: datetime) -> int:
    """Cancella le rigiocate precedenti sullo stesso periodo.

    Solo quelle marcate `origine: rigiocata_storica`: gli scenari veri, generati
    in tempo reale, non vengono toccati in nessun caso.
    """
    candidati = session.execute(
        select(Scenario).where(
            Scenario.generated_at >= start, Scenario.generated_at <= end
        )
    ).scalars().all()

    rimossi = 0
    for scenario in candidati:
        payload = scenario.payload if isinstance(scenario.payload, dict) else {}
        if payload.get("origine") != ORIGINE:
            continue
        # L'eventuale esito segue lo scenario: la relazione ha delete-orphan.
        session.delete(scenario)
        rimossi += 1
    session.flush()
    return rimossi
