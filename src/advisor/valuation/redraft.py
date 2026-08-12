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
from advisor.players import player_profile
from advisor.scoring.projections import positional_scarcity, project_player
from advisor.valuation.aging import relative_multiplier
from advisor.valuation.base import PickValue, PlayerValue, RosterValue

# Cache replacement levels per (league, position, season) — computing one scans
# every stat line at that position, and a roster valuation asks for the same
# handful of positions repeatedly.
_replacement_cache: dict[tuple[str, str, int], float] = {}


def replacement_level(ctx: LeagueContext, position: str | None) -> float:
    """Replacement level, measured against the most recent season with data.

    In the offseason `ctx.season` has no stats at all, so scarcity has to be
    read off `stats_season` or every position would come back at zero and every
    player would look like a league-winner.
    """
    if not position:
        return 0.0
    season = ctx.stats_season or ctx.season
    key = (ctx.league_id, position, season)
    if key not in _replacement_cache:
        _replacement_cache[key] = positional_scarcity(
            ctx.league_id, position, season
        ).replacement_points_per_game
    return _replacement_cache[key]


def clear_caches() -> None:
    """Drop memoised replacement levels. Used by tests and after re-ingesting."""
    _replacement_cache.clear()


def player_snapshot(player_id: str, ctx: LeagueContext) -> dict:
    """Name, position, age, and per-game production for one player.

    Identity comes from `player_profile`, which falls back to earlier seasons —
    without it, an offseason valuation finds no row for the upcoming season and
    silently prices every player at zero.
    """
    profile = player_profile(player_id, ctx.season)
    projection = project_player(
        ctx.league_id,
        player_id,
        ctx.season,
        through_week=ctx.current_week,
        games_remaining=ctx.games_remaining,
    )

    points_per_game = projection.points_per_game
    position = profile.get("position") or projection.position
    age = profile.get("age")

    # Projecting across a season boundary (offseason: 2025 data, 2026 league)
    # has to age the player. This is a PROJECTION concern, not a valuation one:
    # a 30-year-old back will not repeat his age-29 season in either format.
    # It is distinct from dynasty's `future`, which values later seasons.
    seasons_ahead = ctx.season - (ctx.stats_season or ctx.season)
    if seasons_ahead > 0 and age is not None:
        points_per_game *= relative_multiplier(
            position, age - seasons_ahead, seasons_ahead
        )

    return {
        "name": profile.get("full_name") or player_id,
        "position": position,
        "age": age,
        "points_per_game": round(points_per_game, 2),
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
    # Resolved against the latest season with data: in the offseason there are
    # no rows for the season being valued.
    mapped = query(
        f"SELECT player_id FROM players WHERE season = ? "
        f"AND sleeper_id IN ({placeholders})",
        [ctx.stats_season or ctx.season, *[str(s) for s in sleeper_ids]],
    )
    return [r["player_id"] for r in mapped]
