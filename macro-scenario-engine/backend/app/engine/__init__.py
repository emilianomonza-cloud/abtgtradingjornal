"""Motore macro: catene causali, sorprese, scoring valutario, scenari."""

from .causal_chains import (  # noqa: F401
    CHAINS,
    ChainActivation,
    all_chains,
    get_chain,
    inflation_chain_for_regime,
    merge_activations,
)
from .playbooks import (  # noqa: F401
    all_playbooks,
    expected_reaction,
    no_trade_window,
    playbook_for,
    window_for_event,
)
from .scenarios import (  # noqa: F401
    classify_bias,
    generate_all,
    generate_pair_scenarios,
    persist_scenario,
)
from .scoring import (  # noqa: F401
    CurrencyEvaluation,
    SubScore,
    evaluate_all,
    evaluate_currency,
    persist_scores,
    score_change_24h,
)
from .surprise import (  # noqa: F401
    SurpriseResult,
    apply_and_store,
    compute_surprise,
    decay_factor,
)
from .taxonomy import (  # noqa: F401
    classify_category,
    normalize_impact,
    parse_value,
)
