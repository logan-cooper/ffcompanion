"""compare_players — side-by-side weekly production, usage, and value."""

from __future__ import annotations

from advisor.context import LeagueContext
from advisor.db import query
from advisor.tools.base import envelope, error, owner_of, player_index, league_owners
from advisor.valuation import get_valuation

MAX_PLAYERS = 4
MAX_WEEKS = 8


def _usage(ctx: LeagueContext, player_id: str, weeks: int) -> dict:
    season = ctx.stats_season or ctx.season
    rows = query(
        """
        SELECT AVG(snap_share) AS snap, AVG(target_share) AS target,
               AVG(red_zone_touches) AS red_zone
        FROM (
            SELECT * FROM player_week_usage
            WHERE player_id = ? AND season = ? AND season_type = 'REG'
            ORDER BY week DESC LIMIT ?
        )
        """,
        [player_id, season, weeks],
    )
    if not rows or rows[0]["snap"] is None and rows[0]["target"] is None:
        return {}
    row = rows[0]
    usage = {}
    if row["snap"] is not None:
        usage["snap_share"] = round(row["snap"], 3)
    if row["target"] is not None:
        usage["target_share"] = round(row["target"], 3)
    if row["red_zone"] is not None:
        usage["red_zone_touches_per_game"] = round(row["red_zone"], 2)
    return usage


def _next_opponent(ctx: LeagueContext, team: str | None) -> dict | None:
    """Upcoming matchup and how that defense treats the position.

    Returns None once the season is over — there is no next opponent, and
    inventing one would be exactly the kind of fabricated number this app exists
    to avoid.
    """
    if not team or ctx.season_complete or ctx.is_offseason:
        return None

    rows = query(
        """
        SELECT week, home_team, away_team FROM schedules
        WHERE season = ? AND week > ? AND game_type = 'REG'
          AND (home_team = ? OR away_team = ?)
        ORDER BY week LIMIT 1
        """,
        [ctx.season, ctx.current_week, team, team],
    )
    if not rows:
        return None
    row = rows[0]
    opponent = row["away_team"] if row["home_team"] == team else row["home_team"]
    return {"week": row["week"], "opponent": opponent}


def _defense_rank(ctx: LeagueContext, opponent: str, position: str | None) -> int | None:
    if not opponent or not position:
        return None
    rows = query(
        "SELECT defense_rank FROM v_position_defense_rank "
        "WHERE season = ? AND defense_team = ? AND position = ?",
        [ctx.stats_season or ctx.season, opponent, position],
    )
    return rows[0]["defense_rank"] if rows else None


def compare_players(
    ctx: LeagueContext,
    player_ids: list[str],
    *,
    weeks: int = MAX_WEEKS,
) -> dict:
    """Up to four players, most-recent weeks first."""
    if not player_ids:
        return {**envelope(ctx), **error("no players given", "pass 1-4 player_ids")}

    weeks = max(1, min(weeks, MAX_WEEKS))
    requested = player_ids[:MAX_PLAYERS]
    index = player_index(ctx)
    valuation = get_valuation(ctx)
    owners = owner_of(ctx)
    teams = league_owners(ctx)

    missing = [pid for pid in requested if pid not in index]
    found = [pid for pid in requested if pid in index]
    if not found:
        return {
            **envelope(ctx),
            **error(
                "none of those players have stats",
                f"unknown player_ids: {missing}; call resolve_player first",
            ),
        }

    comparison = []
    for player_id in found:
        line = index[player_id]
        value = valuation.player_value(player_id, ctx)
        recent = line.weekly[-weeks:]

        entry = {
            "player_id": player_id,
            "name": line.name,
            "position": line.position,
            "team": line.team,
            "games": line.games,
            "season_points": line.total_points,
            "points_per_game": line.points_per_game,
            "last3_points_per_game": line.last3_points_per_game,
            "weekly_points": [{"week": w, "points": p} for w, p in recent],
            "win_now": value.win_now,
            "future": value.future,
        }
        if line.age is not None:
            entry["age"] = round(line.age, 1)

        usage = _usage(ctx, player_id, weeks)
        if usage:
            entry["usage"] = usage

        owner_roster = owners.get(str(line.sleeper_id)) if line.sleeper_id else None
        entry["owner"] = (
            {"roster_id": owner_roster, "team": teams.get(owner_roster, "?")}
            if owner_roster
            else "free agent"
        )

        upcoming = _next_opponent(ctx, line.team)
        if upcoming:
            upcoming["defense_rank_vs_position"] = _defense_rank(
                ctx, upcoming["opponent"], line.position
            )
            upcoming["rank_note"] = "1 = fewest yards allowed = toughest matchup"
            entry["upcoming"] = upcoming

        comparison.append(entry)

    payload = {**envelope(ctx), "weeks_shown": weeks, "players": comparison}
    if missing:
        payload["not_found"] = missing
    return payload
