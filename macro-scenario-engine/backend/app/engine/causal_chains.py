"""SEZIONE 3.1 — Catene causali esplicite, tracciabili e pesabili.

Ogni catena e' un oggetto con i passi dichiarati in chiaro: quando un sotto-score
la attiva registra un `ChainActivation`, che viene poi mostrato in dashboard, in API
e negli scenari. La spiegabilita' non e' opzionale: se una valutazione non sa dire
QUALE catena l'ha prodotta, non deve essere mostrata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

APPREZZAMENTO = "APPREZZAMENTO"
DEPREZZAMENTO = "DEPREZZAMENTO"
DIPENDE = "DIPENDE_DAL_CONTESTO"


@dataclass(frozen=True)
class CausalChain:
    """Definizione statica di una catena causa → effetto."""

    id: str
    nome: str
    passi_positivi: List[str]
    effetto_positivo: str
    passi_negativi: List[str]
    effetto_negativo: str
    note: str = ""

    def steps(self, direction: int) -> List[str]:
        return list(self.passi_positivi if direction >= 0 else self.passi_negativi)

    def effect(self, direction: int) -> str:
        return self.effetto_positivo if direction >= 0 else self.effetto_negativo

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "nome": self.nome,
            "ramo_positivo": {"passi": self.passi_positivi, "effetto": self.effetto_positivo},
            "ramo_negativo": {"passi": self.passi_negativi, "effetto": self.effetto_negativo},
            "note": self.note,
        }


@dataclass
class ChainActivation:
    """Istanza di attivazione di una catena da parte di un sotto-score."""

    chain_id: str
    direction: int  # +1 ramo positivo, -1 ramo negativo
    strength: float  # 0.0 - 1.0
    trigger: str  # cosa l'ha attivata (evento, documento, differenziale...)
    currency: str = ""
    sources: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        chain = CHAINS[self.chain_id]
        return {
            "id": self.chain_id,
            "nome": chain.nome,
            "valuta": self.currency,
            "direzione": "positiva" if self.direction >= 0 else "negativa",
            "effetto_valuta": chain.effect(self.direction),
            "intensita": round(max(0.0, min(1.0, self.strength)), 2),
            "trigger": self.trigger,
            "passi": chain.steps(self.direction),
            "fonti": self.sources,
        }


# --------------------------------------------------------------------------- #
#  Registro delle catene (SEZIONE 3.1)
# --------------------------------------------------------------------------- #

_CHAIN_LIST: List[CausalChain] = [
    CausalChain(
        id="TASSI_CAPITALI_CAMBIO",
        nome="Tassi → capitali → cambio",
        passi_positivi=[
            "i interno ↑ rispetto a i estero (differenziale tassi ↑)",
            "attivita' finanziarie nazionali piu' attraenti",
            "afflussi di capitale ↑",
            "domanda della valuta ↑",
            "APPREZZAMENTO della valuta",
        ],
        effetto_positivo=APPREZZAMENTO,
        passi_negativi=[
            "i interno ↓ rispetto a i estero (differenziale tassi ↓)",
            "attivita' finanziarie nazionali meno attraenti",
            "deflussi di capitale ↑",
            "domanda della valuta ↓",
            "DEPREZZAMENTO della valuta",
        ],
        effetto_negativo=DEPREZZAMENTO,
        note="Canale piu' diretto e piu' rapido sul cambio.",
    ),
    CausalChain(
        id="MONETARIA_ESPANSIVA",
        nome="Politica monetaria espansiva (cambi flessibili)",
        passi_positivi=[
            "Offerta di moneta ↑ / guidance accomodante",
            "i ↓",
            "investimenti ↑ → domanda aggregata ↑ → PIL ↑",
            "MA i ↓ → minore afflusso di capitali → bilancia dei pagamenti peggiora",
            "la valuta si DEPREZZA",
            "esportazioni ↑, importazioni ↓ → NX ↑ → domanda aggregata ↑ → PIL ↑",
        ],
        effetto_positivo=DEPREZZAMENTO,
        passi_negativi=[
            "Ritiro dello stimolo monetario / guidance meno accomodante",
            "i ↑ rispetto allo scenario precedente",
            "riduzione della spinta al deprezzamento",
            "la valuta recupera terreno",
        ],
        effetto_negativo=APPREZZAMENTO,
        note=(
            "Effetto sulla valuta = DEPREZZAMENTO; effetto sul PIL = positivo con ritardo. "
            "Il ramo 'positivo' della catena indica espansione monetaria in atto."
        ),
    ),
    CausalChain(
        id="MONETARIA_RESTRITTIVA",
        nome="Politica monetaria restrittiva",
        passi_positivi=[
            "Inflazione troppo alta → la banca centrale restringe",
            "i ↑",
            "afflussi di capitale ↑ → APPREZZAMENTO della valuta",
            "investimenti ↓ → domanda aggregata ↓ → PIL rallenta",
            "esportazioni penalizzate dal cambio forte",
        ],
        effetto_positivo=APPREZZAMENTO,
        passi_negativi=[
            "Fine del ciclo restrittivo / primi tagli",
            "i ↓",
            "riduzione degli afflussi di capitale",
            "DEPREZZAMENTO della valuta",
        ],
        effetto_negativo=DEPREZZAMENTO,
        note="Costo della stretta: crescita piu' debole e competitivita' penalizzata.",
    ),
    CausalChain(
        id="PIL_CRESCITA",
        nome="PIL / crescita",
        passi_positivi=[
            "PIL ↑ → produzione ↑ → occupazione ↑ → redditi ↑ → consumi ↑",
            "crescita solida + attese di banca centrale restrittiva",
            "valuta FORTE (effetto indiretto, via canale tassi)",
        ],
        effetto_positivo=APPREZZAMENTO,
        passi_negativi=[
            "PIL ↓ → produzione ↓ → occupazione ↓ → redditi ↓ → consumi ↓",
            "crescita debole → aspettative di easing",
            "valuta DEBOLE",
        ],
        effetto_negativo=DEPREZZAMENTO,
        note=(
            "Effetto sulla valuta INDIRETTO e dipendente dal contesto: crescita trainata da "
            "domanda interna con importazioni ↑ puo' peggiorare la bilancia commerciale."
        ),
    ),
    CausalChain(
        id="INFLAZIONE_COMPETITIVITA",
        nome="Inflazione → competitivita'",
        passi_positivi=[
            "Prezzi interni ↑ piu' dei prezzi esteri (a cambio nominale costante)",
            "il cambio reale si apprezza → competitivita' ↓ → esportazioni ↓",
            "REGIME HAWKISH_DOMINANT: inflazione ↑ → attese di i ↑ → canale capitali dominante",
            "valuta ↑ nel breve termine",
        ],
        effetto_positivo=APPREZZAMENTO,
        passi_negativi=[
            "Inflazione in calo / sotto il target",
            "attese di politica monetaria piu' accomodante",
            "differenziale tassi in compressione",
            "valuta ↓",
        ],
        effetto_negativo=DEPREZZAMENTO,
        note=(
            "In regime CREDIBILITY_LOSS il canale dominante si inverte: inflazione ↑ con banca "
            "centrale bloccata → perdita di competitivita' → valuta ↓. Il regime attivo e' "
            "dichiarato in ogni valutazione."
        ),
    ),
    CausalChain(
        id="INFLAZIONE_CREDIBILITA",
        nome="Inflazione → perdita di credibilita' (regime CREDIBILITY_LOSS)",
        passi_positivi=[
            "Prezzi interni ↑ molto piu' dei prezzi esteri",
            "banca centrale bloccata o non credibile: nessuna risposta sui tassi",
            "cambio reale si apprezza → competitivita' ↓ → esportazioni ↓",
            "canale competitivita' DOMINANTE → valuta ↓",
        ],
        effetto_positivo=DEPREZZAMENTO,
        passi_negativi=[
            "Inflazione rientra verso il target",
            "riduzione del premio al rischio",
            "valuta stabilizzata o in recupero",
        ],
        effetto_negativo=APPREZZAMENTO,
        note="Ramo alternativo della catena inflazione, attivo solo in regime CREDIBILITY_LOSS.",
    ),
    CausalChain(
        id="BILANCIA_PAGAMENTI",
        nome="Bilancia dei pagamenti",
        passi_positivi=[
            "BP > 0 (avanzo): saldo commerciale + movimenti finanziari positivi",
            "domanda della valuta ↑",
            "APPREZZAMENTO",
        ],
        effetto_positivo=APPREZZAMENTO,
        passi_negativi=[
            "BP < 0 (disavanzo)",
            "offerta della valuta ↑ rispetto alla domanda",
            "DEPREZZAMENTO",
        ],
        effetto_negativo=DEPREZZAMENTO,
        note="In regime di cambi fissi lo squilibrio si scarica su riserve valutarie e base monetaria.",
    ),
    CausalChain(
        id="COMMERCIO_ESTERO",
        nome="Commercio estero",
        passi_positivi=[
            "Esportazioni ↑ (o PIL estero ↑ → esportazioni ↑)",
            "gli acquirenti esteri devono comprare la valuta nazionale",
            "domanda della valuta nazionale ↑",
            "APPREZZAMENTO",
        ],
        effetto_positivo=APPREZZAMENTO,
        passi_negativi=[
            "Importazioni ↑ / esportazioni ↓",
            "offerta della valuta nazionale ↑",
            "DEPREZZAMENTO",
        ],
        effetto_negativo=DEPREZZAMENTO,
        note="Canale lento: rilevante sull'orizzonte settimanale, quasi nullo intraday.",
    ),
]

CHAINS: Dict[str, CausalChain] = {c.id: c for c in _CHAIN_LIST}


def get_chain(chain_id: str) -> CausalChain:
    if chain_id not in CHAINS:
        raise KeyError(f"Catena causale sconosciuta: {chain_id}")
    return CHAINS[chain_id]


def inflation_chain_for_regime(regime: str) -> str:
    """Restituisce la catena inflazione corretta per il regime attivo."""
    return (
        "INFLAZIONE_CREDIBILITA"
        if str(regime).upper() == "CREDIBILITY_LOSS"
        else "INFLAZIONE_COMPETITIVITA"
    )


def merge_activations(activations: List[ChainActivation]) -> List[Dict[str, object]]:
    """Aggrega le attivazioni per (catena, direzione) sommando le intensita'.

    Evita che la stessa catena compaia dieci volte nello stesso scenario.
    """
    merged: Dict[tuple, ChainActivation] = {}
    for act in activations:
        key = (act.chain_id, 1 if act.direction >= 0 else -1)
        if key in merged:
            existing = merged[key]
            existing.strength = min(1.0, existing.strength + act.strength * 0.5)
            for src in act.sources:
                if src not in existing.sources:
                    existing.sources.append(src)
            if act.trigger not in existing.trigger:
                existing.trigger = f"{existing.trigger}; {act.trigger}"[:400]
        else:
            merged[key] = ChainActivation(
                chain_id=act.chain_id,
                direction=1 if act.direction >= 0 else -1,
                strength=act.strength,
                trigger=act.trigger,
                currency=act.currency,
                sources=list(act.sources),
            )
    ordered = sorted(merged.values(), key=lambda a: a.strength, reverse=True)
    return [a.to_dict() for a in ordered]


def all_chains() -> List[Dict[str, object]]:
    return [c.to_dict() for c in _CHAIN_LIST]


def find_chain_for_category(category: str, indicators_config: Dict[str, object]) -> Optional[str]:
    """Catena dichiarata in indicators.yaml per una categoria di indicatore."""
    categories = indicators_config.get("categorie", {}) or {}
    spec = categories.get(category) or {}
    chain_id = spec.get("catena") if isinstance(spec, dict) else None
    return chain_id if chain_id in CHAINS else None
