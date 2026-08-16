"""Test unitari sulle catene causali (SEZIONE 3.1) e sulla loro attivazione."""

from __future__ import annotations

import pytest

from app.engine import scoring
from app.engine.causal_chains import (
    APPREZZAMENTO,
    CHAINS,
    ChainActivation,
    inflation_chain_for_regime,
    merge_activations,
)
from conftest import make_event, set_rate


def test_all_chains_are_declared_with_both_branches():
    assert len(CHAINS) >= 7
    for chain in CHAINS.values():
        assert chain.passi_positivi and chain.passi_negativi
        assert chain.effetto_positivo != chain.effetto_negativo


def test_rate_chain_positive_branch_means_appreciation():
    """i interno ↑ → afflussi di capitale → APPREZZAMENTO."""
    chain = CHAINS["TASSI_CAPITALI_CAMBIO"]
    assert chain.effect(1) == APPREZZAMENTO
    steps = chain.steps(1)
    assert "afflussi di capitale ↑" in steps
    assert steps[-1].startswith("APPREZZAMENTO")


def test_expansive_monetary_chain_depreciates_the_currency():
    """Politica monetaria espansiva: PIL ↑ ma valuta ↓ (cambi flessibili)."""
    chain = CHAINS["MONETARIA_ESPANSIVA"]
    assert chain.effect(1) == "DEPREZZAMENTO"
    steps = " | ".join(chain.steps(1))
    assert "i ↓" in steps
    assert "esportazioni ↑" in steps


def test_restrictive_chain_appreciates_and_slows_growth():
    chain = CHAINS["MONETARIA_RESTRITTIVA"]
    assert chain.effect(1) == APPREZZAMENTO
    steps = " | ".join(chain.steps(1))
    assert "PIL rallenta" in steps


def test_inflation_chain_depends_on_regime():
    assert inflation_chain_for_regime("HAWKISH_DOMINANT") == "INFLAZIONE_COMPETITIVITA"
    assert inflation_chain_for_regime("CREDIBILITY_LOSS") == "INFLAZIONE_CREDIBILITA"
    # Nei due regimi lo stesso ramo produce effetti opposti sul cambio.
    assert CHAINS["INFLAZIONE_COMPETITIVITA"].effect(1) == APPREZZAMENTO
    assert CHAINS["INFLAZIONE_CREDIBILITA"].effect(1) == "DEPREZZAMENTO"


def test_balance_of_payments_chain():
    chain = CHAINS["BILANCIA_PAGAMENTI"]
    assert chain.effect(1) == APPREZZAMENTO  # avanzo
    assert chain.effect(-1) == "DEPREZZAMENTO"  # disavanzo


def test_merge_activations_groups_by_chain_and_direction():
    activations = [
        ChainActivation("PIL_CRESCITA", 1, 0.4, "PIL", "USD", ["a"]),
        ChainActivation("PIL_CRESCITA", 1, 0.4, "NFP", "USD", ["b"]),
        ChainActivation("PIL_CRESCITA", -1, 0.3, "Retail", "EUR", ["c"]),
    ]
    merged = merge_activations(activations)
    assert len(merged) == 2
    positive = next(m for m in merged if m["direzione"] == "positiva")
    assert positive["intensita"] > 0.4
    assert set(positive["fonti"]) == {"a", "b"}


def test_rate_differential_activates_the_rate_chain(session):
    set_rate(session, "USD", 6.0)
    for ccy in ("EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"):
        set_rate(session, ccy, 0.5)

    evaluation = scoring.evaluate_currency(session, "USD")
    sub = evaluation.subscores["rate_differential"]
    assert sub.value > 50
    assert sub.chains[0].chain_id == "TASSI_CAPITALI_CAMBIO"
    assert sub.chains[0].direction == 1


def test_growth_chain_activated_by_employment_surprise(session):
    make_event(
        session,
        title="Non-Farm Employment Change",
        currency="USD",
        actual=350.0,
        forecast=180.0,
        previous=175.0,
    )
    from app.engine.surprise import apply_and_store
    from app.storage.models import Event

    for event in session.query(Event).all():
        apply_and_store(session, event)
    session.flush()

    evaluation = scoring.evaluate_currency(session, "USD")
    chains = {c.chain_id for c in evaluation.subscores["growth_momentum"].chains}
    assert "PIL_CRESCITA" in chains
    assert evaluation.subscores["growth_momentum"].value > 0


def test_every_activation_is_explainable(session):
    set_rate(session, "USD", 4.0)
    evaluation = scoring.evaluate_currency(session, "USD")
    for activation in evaluation.chains:
        payload = activation.to_dict()
        assert payload["id"] in CHAINS
        assert payload["passi"], "una catena attivata deve dichiarare i suoi passi"
        assert payload["trigger"], "una catena attivata deve dichiarare cosa l'ha attivata"
