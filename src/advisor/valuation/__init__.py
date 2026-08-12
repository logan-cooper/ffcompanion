"""What a player or pick is worth, format-aware. Phase 3b.

`get_valuation(ctx)` is the **only** entry point tools use, and the only place
format is branched on. If a tool ever needs `if ctx.format == "dynasty"`, the
interface is wrong and should be fixed here instead.
"""

from __future__ import annotations

from advisor.context import LeagueContext
from advisor.league_format import DYNASTY, KEEPER, SURVIVAL, UNKNOWN
from advisor.valuation.base import PickValue, PlayerValue, RosterValue, Valuation
from advisor.valuation.dynasty import DynastyValuation
from advisor.valuation.intent import WeightedValue, combined_value, weigh
from advisor.valuation.redraft import RedraftValuation, clear_caches

# Keeper leagues carry players over, so future value is real — the horizon is
# shorter than dynasty, but treating them as redraft would be more wrong than
# treating them as dynasty.
_MULTI_YEAR = frozenset({DYNASTY, KEEPER})


def get_valuation(ctx: LeagueContext) -> Valuation:
    """Pick the strategy for this league.

    `unknown` and `survival` both fall back to redraft: it is the conservative
    answer (no speculative future value), and in the unknown case the app is
    required to ask the user before giving advice anyway.
    """
    if ctx.format in _MULTI_YEAR:
        return DynastyValuation()
    return RedraftValuation()


__all__ = [
    "DynastyValuation",
    "PickValue",
    "PlayerValue",
    "RedraftValuation",
    "RosterValue",
    "Valuation",
    "WeightedValue",
    "clear_caches",
    "combined_value",
    "get_valuation",
    "weigh",
    "UNKNOWN",
    "SURVIVAL",
]
