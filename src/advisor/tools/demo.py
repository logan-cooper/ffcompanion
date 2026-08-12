"""`make tools-demo` — run all six tools and print what the model would see.

The point is the side-by-side: the *same tool calls with the same arguments* are
run under a dynasty context and a redraft one. Tool signatures are identical
across formats; only what the numbers mean changes. If the two columns were ever
identical, the format abstraction would be decorative.

The roadmap's real test is human: read the raw output and answer a trade
question yourself. If you can't, the model won't be able to either.
"""

from __future__ import annotations

import dataclasses
import json

from advisor.context import LeagueContext, load_context
from advisor.db import query
from advisor.tools import (
    compare_players,
    evaluate_trade,
    get_available_players,
    get_league_rosters,
    get_my_roster,
    resolve_player,
)
from advisor.tools.base import clear_index_cache, estimated_tokens

# Mid-season so rest-of-season value is non-zero and the output is worth reading.
DEMO_WEEK = 14


def _first_id(name: str, ctx: LeagueContext) -> str | None:
    result = resolve_player(name, ctx)
    candidates = result.get("candidates") or []
    return candidates[0]["player_id"] if candidates else None


def _print(title: str, payload: dict, *, full: bool = False) -> None:
    print(f"\n{'-' * 78}\n{title}   (~{estimated_tokens(payload)} tokens)\n{'-' * 78}")
    if "error" in payload:
        print(f"  ERROR: {payload['error']}\n  detail: {payload['detail']}")
        return
    text = json.dumps(payload, indent=2, default=str)
    print(text if full else text[:1800] + ("\n  ...[truncated for display]" if len(text) > 1800 else ""))


def run_demo(league_id: str | None = None, roster_id: int | None = None) -> int:
    clear_index_cache()

    if league_id is None:
        # Prefer a league where a team intent has actually been set — otherwise
        # the demo shows the `balanced` default and the intent weighting looks
        # inert.
        rows = query(
            """
            SELECT l.league_id, t.roster_id
            FROM leagues l
            LEFT JOIN team_intent t ON t.league_id = l.league_id
            WHERE l.format = 'dynasty'
            ORDER BY t.roster_id IS NULL, l.name
            LIMIT 1
            """
        )
        if not rows:
            print("No dynasty league linked. Run: make link-league USERNAME=... SEASON=2025")
            return 1
        league_id = rows[0]["league_id"]
        if roster_id is None:
            roster_id = rows[0]["roster_id"]

    if roster_id is None:
        roster_id = 1

    dynasty = load_context(league_id, roster_id=roster_id, current_week=DEMO_WEEK)
    # Same league, same data, reinterpreted as a single-year format. This is the
    # comparison the phase gate asks for.
    redraft = dataclasses.replace(dynasty, format="redraft", name=dynasty.name + " (as redraft)")

    give = _first_id("Christian McCaffrey", dynasty)
    get = _first_id("Tetairoa McMillan", dynasty)
    compare = [i for i in (_first_id("Puka Nacua", dynasty), give) if i]

    for ctx in (dynasty, redraft):
        print(f"\n{'=' * 78}")
        print(f"  {ctx.name}   format={ctx.format}  intent={ctx.team_intent}  "
              f"superflex={ctx.superflex}  week={ctx.current_week}")
        print("=" * 78)

        _print("1. resolve_player('nacua')", resolve_player("nacua", ctx), full=True)
        _print("2. get_my_roster", get_my_roster(ctx, roster_id))
        _print("3. get_league_rosters", get_league_rosters(ctx))
        if compare:
            _print("4. compare_players", compare_players(ctx, compare, weeks=4))
        _print("5. get_available_players(RB)", get_available_players(ctx, "RB", limit=5))
        if give and get:
            _print(
                "6. evaluate_trade  (give McCaffrey 29 / get McMillan 22 + 2027-1st)",
                evaluate_trade(ctx, roster_id, 1, [give], [get, "2027-1st"]),
                full=True,
            )

    print(f"\n{'=' * 78}")
    print("Same arguments, both formats. Compare `future` and the trade deltas:")
    print("dynasty prices youth and picks; redraft zeroes both by definition.")
    print("=" * 78)
    return 0
