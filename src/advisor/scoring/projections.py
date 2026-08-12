"""Rest-of-season projections and positional scarcity.

**Deliberately dumb.** A weighted blend of recent form and season average,
nudged by opponent strength. No model, no training, no black box.

The value of this app is grounded reasoning over real usage data, not projection
accuracy. A transparent heuristic that can be explained in one sentence beats a
better-fitting model that cannot, because every number the assistant states has
to be defensible. Every knob below is a named constant for that reason.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from advisor.db import query
from advisor.scoring.engine import score_stat_line

# Recent form vs season-long. Recency wins the tiebreak because usage changes
# (injuries, depth-chart moves) show up in the last three games first.
RECENT_WEIGHT = 0.6
SEASON_WEIGHT = 0.4

RECENT_GAMES = 3

# How far opponent strength is allowed to move a projection. A defense ranked
# 1st (toughest) scales by (1 - SWING), 32nd by (1 + SWING). Kept small on
# purpose: matchup matters less than volume, and overstating it is the classic
# way a projection starts lying confidently.
OPPONENT_SWING = 0.15

# Positions that can fill a FLEX slot, and the ones a SUPER_FLEX realistically
# gets used on.
FLEX_POSITIONS = ("RB", "WR", "TE")
SUPERFLEX_POSITION = "QB"

REGULAR_SEASON_WEEKS = 18


@dataclass
class Projection:
    """A projection with its inputs exposed, so it can be explained."""

    player_id: str
    position: str | None
    games_played: int
    season_avg: float
    recent_avg: float
    points_per_game: float
    games_remaining: int
    total: float

    def explain(self) -> str:
        return (
            f"{self.points_per_game:.1f} pts/gm "
            f"({RECENT_WEIGHT:.0%} x last{RECENT_GAMES} {self.recent_avg:.1f} + "
            f"{SEASON_WEIGHT:.0%} x season {self.season_avg:.1f}) "
            f"x {self.games_remaining} games = {self.total:.1f}"
        )


def _scoring_settings(league_id: str) -> dict:
    rows = query(
        "SELECT scoring_settings FROM leagues WHERE league_id = ?", [league_id]
    )
    if not rows:
        raise LookupError(f"league {league_id!r} is not linked")
    return json.loads(rows[0]["scoring_settings"])


def weekly_points(
    league_id: str, player_id: str, season: int, *, through_week: int | None = None
) -> list[tuple[int, float]]:
    """This player's scored weeks under this league's rules, oldest first."""
    settings = _scoring_settings(league_id)
    clause = " AND s.week <= ?" if through_week else ""
    params = [season, player_id] + ([through_week] if through_week else [])

    rows = query(
        f"""
        SELECT s.*, p.position
        FROM player_week_stats s
        LEFT JOIN players p ON p.player_id = s.player_id AND p.season = s.season
        WHERE s.season = ? AND s.player_id = ? AND s.season_type = 'REG'{clause}
        ORDER BY s.week
        """,
        params,
    )
    return [
        (r["week"], score_stat_line(r, settings, position=r["position"])) for r in rows
    ]


def opponent_adjustment(defense_rank: int | None) -> float:
    """Scale factor from a 1-32 defensive rank. 1 = toughest matchup.

    Linear between (1 - OPPONENT_SWING) and (1 + OPPONENT_SWING).
    """
    if not defense_rank:
        return 1.0
    fraction = (defense_rank - 1) / 31  # 0.0 at rank 1, 1.0 at rank 32
    return (1 - OPPONENT_SWING) + fraction * (2 * OPPONENT_SWING)


def project_player(
    league_id: str,
    player_id: str,
    season: int,
    *,
    through_week: int | None = None,
    games_remaining: int | None = None,
) -> Projection:
    """Rest-of-season projection for one player under one league's rules."""
    scored = weekly_points(league_id, player_id, season, through_week=through_week)
    position_rows = query(
        "SELECT position FROM players WHERE player_id = ? AND season = ?",
        [player_id, season],
    )
    position = position_rows[0]["position"] if position_rows else None

    if not scored:
        return Projection(player_id, position, 0, 0.0, 0.0, 0.0, 0, 0.0)

    points = [p for _, p in scored]
    season_avg = sum(points) / len(points)
    recent = points[-RECENT_GAMES:]
    recent_avg = sum(recent) / len(recent)

    per_game = RECENT_WEIGHT * recent_avg + SEASON_WEIGHT * season_avg

    if games_remaining is None:
        last_week = through_week or (scored[-1][0] if scored else 0)
        games_remaining = max(0, REGULAR_SEASON_WEEKS - last_week)

    return Projection(
        player_id=player_id,
        position=position,
        games_played=len(points),
        season_avg=round(season_avg, 2),
        recent_avg=round(recent_avg, 2),
        points_per_game=round(per_game, 2),
        games_remaining=games_remaining,
        total=round(per_game * games_remaining, 2),
    )


# ------------------------------------------------------------------- scarcity


@dataclass
class Scarcity:
    """Replacement level at a position, and how it was derived."""

    league_id: str
    position: str
    starters_league_wide: int
    replacement_points_per_game: float
    starter_count_source: str


def starter_demand(league_id: str) -> dict[str, int]:
    """How many players at each position the league starts in total.

    Derived from `roster_positions` rather than assumed, because that is what
    actually sets replacement level. FLEX slots are spread across RB/WR/TE, and
    a SUPER_FLEX is counted as a QB — it is filled by one in practice, and that
    is precisely why superflex raises QB replacement level so sharply.
    """
    rows = query(
        "SELECT roster_positions, total_rosters FROM leagues WHERE league_id = ?",
        [league_id],
    )
    if not rows:
        raise LookupError(f"league {league_id!r} is not linked")

    positions = json.loads(rows[0]["roster_positions"] or "[]")
    teams = rows[0]["total_rosters"] or 0

    demand: dict[str, float] = {}
    flex_slots = 0
    for slot in positions:
        if slot == "BN" or slot.startswith("IR") or slot == "TAXI":
            continue
        if slot in ("FLEX", "REC_FLEX", "WRRB_FLEX"):
            flex_slots += 1
        elif slot == "SUPER_FLEX":
            demand[SUPERFLEX_POSITION] = demand.get(SUPERFLEX_POSITION, 0) + 1
        else:
            demand[slot] = demand.get(slot, 0) + 1

    # Split flex evenly across eligible positions; no attempt to model which
    # position actually fills it, since that varies week to week.
    if flex_slots:
        share = flex_slots / len(FLEX_POSITIONS)
        for position in FLEX_POSITIONS:
            demand[position] = demand.get(position, 0) + share

    return {position: round(count * teams) for position, count in demand.items()}


def positional_scarcity(
    league_id: str, position: str, season: int, *, min_games: int = 4
) -> Scarcity:
    """Points per game of the first startable player *below* starter demand.

    Trade evaluation is meaningless without this: two players producing the same
    points are not worth the same if one is easily replaced from the wire.
    """
    demand = starter_demand(league_id).get(position, 0)
    settings = _scoring_settings(league_id)

    rows = query(
        """
        SELECT s.*, p.position
        FROM player_week_stats s
        JOIN players p ON p.player_id = s.player_id AND p.season = s.season
        WHERE s.season = ? AND s.season_type = 'REG' AND p.position = ?
        """,
        [season, position],
    )

    per_player: dict[str, list[float]] = {}
    for row in rows:
        per_player.setdefault(row["player_id"], []).append(
            score_stat_line(row, settings, position=position)
        )

    averages = sorted(
        (sum(v) / len(v) for v in per_player.values() if len(v) >= min_games),
        reverse=True,
    )

    if not averages:
        replacement = 0.0
    elif demand < len(averages):
        replacement = averages[demand]  # first player outside the starter pool
    else:
        replacement = averages[-1]

    return Scarcity(
        league_id=league_id,
        position=position,
        starters_league_wide=demand,
        replacement_points_per_game=round(replacement, 2),
        starter_count_source="roster_positions x total_rosters",
    )


def points_above_replacement(
    league_id: str, player_id: str, season: int, **kwargs
) -> float:
    """A player's per-game points above replacement level at their position."""
    projection = project_player(league_id, player_id, season, **kwargs)
    if not projection.position:
        return 0.0
    scarcity = positional_scarcity(league_id, projection.position, season)
    return round(
        projection.points_per_game - scarcity.replacement_points_per_game, 2
    )
