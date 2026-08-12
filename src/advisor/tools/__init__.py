"""The six pure functions the model calls, plus the schema registry. Phase 4.

Tools return data, never prose or opinions, and never a verdict. They also never
branch on league format — they ask `get_valuation(ctx)` and report what comes
back, which is what keeps their signatures identical in redraft and dynasty.
"""

from advisor.tools.base import clear_index_cache, envelope, error
from advisor.tools.compare import compare_players
from advisor.tools.players import resolve_player
from advisor.tools.registry import REGISTRY, TOOLS, tool_names, validate_registry
from advisor.tools.rosters import get_league_rosters, get_my_roster
from advisor.tools.trade import evaluate_trade, parse_pick
from advisor.tools.waivers import get_available_players

__all__ = [
    "REGISTRY",
    "TOOLS",
    "clear_index_cache",
    "compare_players",
    "envelope",
    "error",
    "evaluate_trade",
    "get_available_players",
    "get_league_rosters",
    "get_my_roster",
    "parse_pick",
    "resolve_player",
    "tool_names",
    "validate_registry",
]
