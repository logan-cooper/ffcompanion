"""get_available_players — the waiver wire, ranked by recent production.

Trending-add counts come from Sleeper live. That call is wrapped: a waiver tool
that fails because a third-party endpoint is slow is worse than one that returns
production data without the popularity signal.
"""

from __future__ import annotations

import logging

from advisor.context import LeagueContext
from advisor.db import query
from advisor.tools.base import envelope, error, player_index, truncate

log = logging.getLogger(__name__)

MAX_LIMIT = 15
TRENDING_LOOKBACK_HOURS = 24
TRENDING_POOL = 200

FANTASY_POSITIONS = ("QB", "RB", "WR", "TE", "K")


def _trending() -> dict[str, int]:
    """Sleeper add counts, keyed by sleeper_id. Empty on any failure."""
    try:
        from advisor.sources import sleeper

        rows = sleeper.get_trending_players(
            "add", lookback_hours=TRENDING_LOOKBACK_HOURS, limit=TRENDING_POOL
        )
        return {str(r["player_id"]): r["count"] for r in rows if r.get("player_id")}
    except Exception as exc:  # network, rate limit, schema drift
        log.warning("trending adds unavailable: %s", exc)
        return {}


def get_available_players(
    ctx: LeagueContext,
    position: str | None = None,
    *,
    limit: int = MAX_LIMIT,
) -> dict:
    """Free agents in this league, best recent production first."""
    limit = max(1, min(limit, MAX_LIMIT))

    if position:
        position = position.upper()
        if position not in FANTASY_POSITIONS:
            return {
                **envelope(ctx),
                **error(
                    f"unknown position {position!r}",
                    f"expected one of {list(FANTASY_POSITIONS)}",
                ),
            }

    # No position filter in SQL: `available_players.position` comes from Sleeper
    # while everything downstream (stats, valuation, the position we emit) uses
    # nflverse's. The two disagree often enough that filtering on one and
    # reporting the other returns quarterbacks to a request for tight ends.
    rows = query(
        """
        SELECT a.sleeper_id, a.player_id, a.full_name, a.team
        FROM available_players a
        WHERE a.league_id = ?
        """,
        [ctx.league_id],
    )
    if not rows:
        return {
            **envelope(ctx),
            **error(
                "no free agents found",
                f"league {ctx.league_id} may not be linked; run link-league",
            ),
        }

    index = player_index(ctx)
    trending = _trending()

    candidates = []
    for row in rows:
        line = index.get(row["player_id"]) if row["player_id"] else None
        if line is None or line.games == 0:
            # No production to rank on. Trending-only adds are a Phase 6
            # question; ranking them here would put unknowns above producers.
            continue
        if position and line.position != position:
            continue
        entry = {
            "player_id": line.player_id,
            "name": line.name,
            "position": line.position,
            "team": line.team,
            "games": line.games,
            "season_points": line.total_points,
            "points_per_game": line.points_per_game,
            "last3_points_per_game": line.last3_points_per_game,
            "trend_vs_season": line.trend,
        }
        if line.age is not None:
            entry["age"] = round(line.age, 1)
        adds = trending.get(str(row["sleeper_id"]))
        if adds:
            entry["trending_adds_24h"] = adds
        candidates.append(entry)

    if not candidates:
        return {
            **envelope(ctx),
            **error(
                "no free agents with production",
                f"nobody available at {position or 'any position'} has "
                f"{ctx.stats_season} stats",
            ),
        }

    # Recent form leads: the wire is about who is useful now, not who was.
    candidates.sort(key=lambda c: -c["last3_points_per_game"])
    shown, note = truncate(candidates, limit, "free agents")

    payload = {
        **envelope(ctx),
        "position": position or "all",
        "ranked_by": "last 3 games points per game",
        "available": shown,
    }
    if note:
        payload["truncated"] = note
    if not trending:
        payload["trending_note"] = "Sleeper trending data unavailable"
    return payload
