"""Data for the browsing panels: your roster, the other teams, the wire, the table.

**Same numbers as the answers beside them.** These read the primitives the tools
read — `player_index` for scoring, `get_valuation` for value, `player_entry` for
the shape — because a sidebar quoting 14.2 next to an answer quoting 12.8 for the
same player destroys the one thing this app is selling.

What they do *not* reuse is the tool wrappers. Those exist for a model with an
8k context: they cap lists and shrink payloads to a token budget. A browser is
not a context window, and dropping a dynasty bench to save tokens nobody is
spending is the wrong trade here. Same numbers, no budget.

Nothing in this module is opinionated. Like the tools, it returns data — the
panels rank and label, they never advise.
"""

from __future__ import annotations

from typing import Any

from advisor.context import LeagueContext
from advisor.db import query
from advisor.tools.base import (
    data_as_of,
    league_owners,
    player_index,
    roster_slots,
    _win_now_caveat,
)
from advisor.tools.rosters import player_entry
from advisor.tools.waivers import FANTASY_POSITIONS, available_candidates
from advisor.valuation import get_valuation
from advisor.warehouse.refresh import identify, last_synced

# Slots in `roster_positions` that a starter never occupies. Sleeper's
# `starters` array is positional against everything else.
NON_STARTING_SLOTS = frozenset({"BN", "IR", "TAXI"})

# Sleeper writes "0" into a starting slot nobody is filling.
EMPTY_SLOT = "0"

WAIVER_LIMIT = 40
MAX_WAIVER_LIMIT = 100


def _header(ctx: LeagueContext) -> dict[str, Any]:
    """What every panel says about which league and which moment it is showing."""
    return {
        "league_id": ctx.league_id,
        "league": ctx.name,
        "season": ctx.season,
        "format": ctx.format,
        "superflex": ctx.superflex,
        # The asker's stance, not the viewed team's. Emitted so that staying
        # true is something a test can check rather than something to hope for.
        "team_intent": ctx.team_intent,
        "your_roster_id": ctx.roster_id,
        "current_week": ctx.current_week,
        "stats_season": ctx.stats_season,
        # Two different ages, and conflating them hides a stale roster behind a
        # fresh stats date. Stats come from nflverse weekly; rosters come from
        # Sleeper on every load.
        "data_as_of": data_as_of(ctx),
        "rosters_as_of": last_synced(ctx.league_id),
        # win_now is 0 for everyone once the season ends, by arithmetic. Shown
        # unlabelled in a column of zeroes it reads as a verdict on the player.
        **_win_now_caveat(ctx),
    }


def _lineup_slots(ctx: LeagueContext, starters: list[str]) -> list[str] | None:
    """Label each starter with the slot it fills, or None if we cannot be sure.

    Sleeper's `starters` array is positional: entry *i* fills the *i*-th
    non-bench slot of `roster_positions`. That turns a flat list into an actual
    lineup, which is most of what a roster panel is for.

    Returns None when the lengths disagree rather than labelling anyway. A
    mislabelled slot is a wrong answer wearing a UI, not a cosmetic flaw.
    """
    slots = [p for p in ctx.roster_positions if p not in NON_STARTING_SLOTS]
    return slots if len(slots) == len(starters) else None


def _no_stats_label(ctx: LeagueContext, known: dict) -> str:
    """Say *why* there are no numbers, which is usually more useful than the gap.

    "Practice Squad" and "no NFL team" are answers; a blank 0.0 next to a real
    name is not, and reads as "he was terrible" rather than "he did not play".
    """
    status = (known.get("status") or "").strip()
    if status and status.lower() not in ("active",):
        return status
    if not known.get("team"):
        return "no NFL team"
    return f"no {ctx.stats_season} games"


def roster_panel(ctx: LeagueContext, roster_id: int | None = None) -> dict[str, Any]:
    """One team, whole: lineup, bench, taxi, reserve, picks. Nothing trimmed."""
    roster_id = roster_id if roster_id is not None else ctx.roster_id
    if roster_id is None:
        return {**_header(ctx), "error": "no roster selected"}

    slots = roster_slots(ctx, roster_id)
    if not slots:
        return {**_header(ctx), "error": f"roster {roster_id} not found"}

    index = player_index(ctx)
    by_sleeper = {line.sleeper_id: line for line in index.values() if line.sleeper_id}
    valuation = get_valuation(ctx)

    unknown = [
        sleeper_id
        for group in slots.values()
        for sleeper_id in group
        if sleeper_id != EMPTY_SLOT and sleeper_id not in by_sleeper
    ]
    fallback = identify(unknown)

    def entry(sleeper_id: str) -> dict:
        line = by_sleeper.get(sleeper_id)
        if line is not None:
            return player_entry(line, valuation.player_value(line.player_id, ctx), ctx)
        known = fallback.get(sleeper_id, {})
        return {
            "player_id": None,
            # Last resort names the id rather than shrugging: "(unknown player)"
            # tells the manager nothing they can act on, and it is his roster.
            "name": known.get("full_name") or f"Sleeper player {sleeper_id}",
            "position": known.get("position"),
            "team": known.get("team"),
            "age": known.get("age"),
            "status": known.get("status"),
            "injury_status": known.get("injury_status"),
            "games": 0,
            "no_stats": _no_stats_label(ctx, known),
        }

    starters = slots.get("starters", [])
    lineup = _lineup_slots(ctx, starters)

    started = []
    for position, sleeper_id in zip(lineup or [None] * len(starters), starters):
        if sleeper_id == EMPTY_SLOT:
            started.append({"slot": position, "empty": True})
        else:
            started.append({"slot": position, **entry(sleeper_id)})

    payload = {
        **_header(ctx),
        "roster_id": roster_id,
        "team": league_owners(ctx).get(roster_id, "?"),
        "is_you": roster_id == ctx.roster_id,
        "starters": started,
        # Bench order is arbitrary in Sleeper, so rank it — best first is the
        # only ordering a bench is ever read in.
        "bench": _ranked([entry(s) for s in slots.get("bench", [])]),
        "taxi": _ranked([entry(s) for s in slots.get("taxi", [])]),
        "reserve": _ranked([entry(s) for s in slots.get("reserve", [])]),
    }

    record = query(
        "SELECT wins, losses, ties, fpts FROM league_rosters "
        "WHERE league_id = ? AND roster_id = ?",
        [ctx.league_id, roster_id],
    )
    if record:
        payload["record"] = _record(record[0])
        payload["points_for"] = record[0]["fpts"]

    return payload


def _ranked(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=lambda e: -(e.get("points_per_game") or 0.0))


def _record(row: dict) -> str:
    base = f"{row['wins'] or 0}-{row['losses'] or 0}"
    return f"{base}-{row['ties']}" if row.get("ties") else base


def standings_panel(ctx: LeagueContext) -> dict[str, Any]:
    """The league table, from the record Sleeper keeps."""
    rows = query(
        "SELECT roster_id, wins, losses, ties, fpts FROM league_rosters "
        "WHERE league_id = ?",
        [ctx.league_id],
    )
    if not rows:
        return {**_header(ctx), "error": "this league has no rosters"}

    teams = league_owners(ctx)
    ordered = sorted(
        rows,
        key=lambda r: (-(r["wins"] or 0), (r["losses"] or 0), -(r["fpts"] or 0.0)),
    )

    standings = [
        {
            "rank": rank,
            "roster_id": row["roster_id"],
            "team": teams.get(row["roster_id"], "?"),
            "wins": row["wins"] or 0,
            "losses": row["losses"] or 0,
            "ties": row["ties"] or 0,
            "record": _record(row),
            "points_for": round(row["fpts"] or 0.0, 1),
            "is_you": row["roster_id"] == ctx.roster_id,
        }
        for rank, row in enumerate(ordered, start=1)
    ]

    payload = {**_header(ctx), "standings": standings}
    # Everyone at 0-0 is not a twelve-way tie for first, it is a season that has
    # not started. Ranking it silently would invent a table out of nothing.
    if not any(s["wins"] or s["losses"] or s["ties"] for s in standings):
        payload["note"] = (
            f"no {ctx.season} games played yet — this is roster order, not a table"
        )
    return payload


def waivers_panel(
    ctx: LeagueContext, position: str | None = None, limit: int = WAIVER_LIMIT
) -> dict[str, Any]:
    """The wire, ranked by recent form. Same list the model reads, just longer."""
    limit = max(1, min(limit, MAX_WAIVER_LIMIT))

    if position:
        position = position.upper()
        if position not in FANTASY_POSITIONS:
            return {
                **_header(ctx),
                "error": f"unknown position {position!r}",
                "positions": list(FANTASY_POSITIONS),
            }

    candidates, had_trending = available_candidates(ctx, position)
    if not candidates:
        return {
            **_header(ctx),
            "error": f"no free agents with {ctx.stats_season} production"
            + (f" at {position}" if position else ""),
            "positions": list(FANTASY_POSITIONS),
        }

    payload = {
        **_header(ctx),
        "position": position or "all",
        "positions": list(FANTASY_POSITIONS),
        "ranked_by": "last 3 games points per game",
        "total": len(candidates),
        "available": candidates[:limit],
    }
    if not had_trending:
        payload["trending_note"] = "Sleeper trending data unavailable"
    return payload
