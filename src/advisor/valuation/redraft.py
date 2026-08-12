"""Redraft valuation: only this season exists.

Rosters reset each year, so a 27-year-old and a 22-year-old producing
identically are worth exactly the same. `future` is always 0 and picks are
worth nothing — not because the code ignores them, but because that is the
correct answer in this format.
"""

from __future__ import annotations

import json

from advisor.context import LeagueContext
from advisor.db import query
from advisor.scoring.projections import positional_scarcity, project_player
from advisor.valuation.base import PickValue, PlayerValue, RosterValue

# Cache replacement levels per (league, position, season) — computing one scans
# every stat line at that position, and a roster valuation asks for the same
# handful of positions repeatedly.
_replacement_cache: dict[tuple[str, str, int], float] = {}


def replacement_level(ctx: LeagueContext, position: str | None) -> float:
    if not position:
        return 0.0
    key = (ctx.league_id, position, ctx.season)
    if key not in _replacement_cache:
        _replacement_cache[key] = positional_scarcity(
            ctx.league_id, position, ctx.season
        ).replacement_points_per_game
    return _replacement_cache[key]


def clear_caches() -> None:
    """Drop memoised replacement levels. Used by tests and after re-ingesting."""
    _replacement_cache.clear()


def player_snapshot(player_id: str, ctx: LeagueContext) -> dict:
    """Name, position, age, and per-game production for one player."""
    rows = query(
        "SELECT full_name, position, age FROM players "
        "WHERE player_id = ? AND season = ?",
        [player_id, ctx.season],
    )
    row = rows[0] if rows else {}
    projection = project_player(
        ctx.league_id, player_id, ctx.season, through_week=ctx.current_week
    )
    return {
        "name": row.get("full_name") or player_id,
        "position": row.get("position") or projection.position,
        "age": row.get("age"),
        "points_per_game": projection.points_per_game,
        "games_remaining": projection.games_remaining,
    }


class RedraftValuation:
    """Rest-of-season production above replacement. Nothing else counts."""

    name = "redraft"

    def player_value(self, player_id: str, ctx: LeagueContext) -> PlayerValue:
        snapshot = player_snapshot(player_id, ctx)
        replacement = replacement_level(ctx, snapshot["position"])
        per_game_above = snapshot["points_per_game"] - replacement

        return PlayerValue(
            player_id=player_id,
            name=snapshot["name"],
            position=snapshot["position"],
            age=snapshot["age"],
            # Floored at zero: replacement level is what the waiver wire gives
            # away, so a player below it is worth nothing as an asset rather
            # than being a liability you are forced to start.
            win_now=round(max(0.0, per_game_above) * snapshot["games_remaining"], 2),
            future=0.0,
            points_per_game=snapshot["points_per_game"],
            replacement_points_per_game=replacement,
            detail={"points_above_replacement_per_game": round(per_game_above, 2)},
        )

    def pick_value(self, season: int, round_: int, ctx: LeagueContext) -> PickValue:
        """Picks are not assets in redraft."""
        return PickValue(season=season, round=round_, win_now=0.0, future=0.0)

    def roster_value(self, roster_id: int, ctx: LeagueContext) -> RosterValue:
        return RosterValue(
            roster_id=roster_id,
            players=tuple(
                self.player_value(pid, ctx) for pid in roster_player_ids(roster_id, ctx)
            ),
            picks=(),
        )


def roster_player_ids(roster_id: int, ctx: LeagueContext) -> list[str]:
    """nflverse player ids on a roster, via the Sleeper crosswalk."""
    rows = query(
        "SELECT players FROM league_rosters WHERE league_id = ? AND roster_id = ?",
        [ctx.league_id, roster_id],
    )
    if not rows:
        return []

    sleeper_ids = json.loads(rows[0]["players"] or "[]")
    if not sleeper_ids:
        return []

    placeholders = ", ".join("?" for _ in sleeper_ids)
    mapped = query(
        f"SELECT player_id FROM players WHERE season = ? "
        f"AND sleeper_id IN ({placeholders})",
        [ctx.season, *[str(s) for s in sleeper_ids]],
    )
    return [r["player_id"] for r in mapped]
