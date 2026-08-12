"""Phase 3 gate: points match what the league actually recorded.

The engine is validated two ways. The property test (half-PPR vs full-PPR) pins
the arithmetic. The spot checks pin real player-weeks against the points Sleeper
itself recorded, which is what catches a wrong column mapping — the failure mode
that produces plausible-but-wrong numbers rather than a crash.
"""

from __future__ import annotations

import json

import pytest

from advisor.db import query
from advisor.scoring.engine import score_stat_line, score_stat_line_detailed
from advisor.scoring.keys import FIRST_DOWN_KEYS, STAT_KEY_COLUMNS
from advisor.scoring.projections import (
    OPPONENT_SWING,
    PRIOR_STRENGTH_GAMES,
    RECENT_WEIGHT,
    SEASON_WEIGHT,
    opponent_adjustment,
    positional_scarcity,
    project_player,
    starter_demand,
)

SEASON = 2025

HALF_PPR = {"rec": 0.5, "rec_yd": 0.1, "rec_td": 6, "rush_yd": 0.1, "rush_td": 6,
            "pass_yd": 0.04, "pass_td": 4, "pass_int": -2, "fum_lost": -2}
FULL_PPR = {**HALF_PPR, "rec": 1.0}

# Real player-weeks, checked against the points Sleeper recorded in Wolfpack
# Dynasty (1 PPR, 4-point passing TDs, no first-down or 40+ bonuses).
WOLFPACK_SCORING = {
    "rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0, "rec_2pt": 2.0,
    "rush_yd": 0.1, "rush_td": 6.0, "rush_2pt": 2.0,
    "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -1.0, "pass_2pt": 2.0,
    "fum_lost": -2.0, "st_td": 6.0,
}

SLEEPER_RECORDED = [
    # (name, week, position, points Sleeper recorded)
    ("Christian McCaffrey", 3, "RB", 24.0),
    ("Puka Nacua", 4, "WR", 36.0),
    ("Travis Kelce", 5, "TE", 19.1),
]


def _stat_line(name: str, week: int) -> dict:
    rows = query(
        """
        SELECT s.* FROM player_week_stats s
        JOIN players p ON p.player_id = s.player_id AND p.season = s.season
        WHERE s.season = ? AND s.week = ? AND s.season_type = 'REG'
          AND p.full_name = ?
        """,
        [SEASON, week, name],
    )
    assert len(rows) == 1, f"expected one {name} row in week {week}"
    return rows[0]


# --------------------------------------------------------------- arithmetic

def test_missing_keys_score_zero_rather_than_raising():
    """Sleeper omits any rule left at its default."""
    assert score_stat_line({"receptions": 5}, {}) == 0.0
    assert score_stat_line({}, FULL_PPR) == 0.0


def test_none_and_missing_stats_are_treated_as_zero():
    assert score_stat_line({"receptions": None, "receiving_yards": None}, FULL_PPR) == 0.0


def test_a_rule_set_to_zero_contributes_nothing():
    breakdown = score_stat_line_detailed({"receptions": 8}, {"rec": 0.0})
    assert breakdown.total == 0.0
    assert "rec" not in breakdown.contributions


def test_kicker_and_defense_keys_are_ignored_for_skill_players():
    """A league's settings carry DEF/K rules; they must not be flagged as
    unsupported when scoring a running back."""
    settings = {**FULL_PPR, "sack": 1.0, "pts_allow_0": 10.0, "fgm_40_49": 4.0, "def_td": 6.0}
    breakdown = score_stat_line_detailed({"receptions": 3}, settings)
    assert breakdown.total == 3.0
    assert breakdown.unsupported_keys == set()


def test_unknown_offensive_keys_are_surfaced_not_swallowed():
    """A missing rule is a wrong answer, not a rounding error."""
    breakdown = score_stat_line_detailed({"receptions": 1}, {"rec": 1.0, "rec_moonshot": 5.0})
    assert "rec_moonshot" in breakdown.unsupported_keys


def test_te_premium_applies_only_to_tight_ends():
    stats = {"receptions": 6}
    settings = {"rec": 1.0, "bonus_rec_te": 0.5}

    assert score_stat_line(stats, settings, position="TE") == 9.0
    assert score_stat_line(stats, settings, position="WR") == 6.0
    assert score_stat_line(stats, settings) == 6.0


def test_yardage_milestone_bonus_is_flat_and_threshold_based():
    settings = {"rec_yd": 0.1, "bonus_rec_yd_100": 1.0}

    assert score_stat_line({"receiving_yards": 99}, settings) == pytest.approx(9.9)
    assert score_stat_line({"receiving_yards": 100}, settings) == pytest.approx(11.0)
    assert score_stat_line({"receiving_yards": 150}, settings) == pytest.approx(16.0)


def test_touchdowns_are_not_counted_as_first_downs():
    """nflverse counts a scoring play as a first down; Sleeper does not.

    Left unadjusted this over-scores every touchdown, which is small, plausible
    and everywhere — it was 744 wrong player-weeks in one real league.
    """
    stats = {"receptions": 5, "receiving_first_downs": 4, "receiving_tds": 2}
    assert score_stat_line(stats, {"rec_fd": 0.25}) == pytest.approx(0.5)  # (4-2) x 0.25


def test_first_downs_never_go_negative():
    stats = {"receiving_first_downs": 1, "receiving_tds": 3}
    assert score_stat_line(stats, {"rec_fd": 0.25}) == 0.0


def test_breakdown_sums_to_total():
    stats = {"receptions": 7, "receiving_yards": 104, "receiving_tds": 1}
    breakdown = score_stat_line_detailed(
        stats, {**FULL_PPR, "bonus_rec_yd_100": 1.0}
    )
    assert sum(breakdown.contributions.values()) == pytest.approx(breakdown.total)
    assert breakdown.explain().startswith(f"{breakdown.total:.2f} = ")


def test_first_down_keys_are_not_also_plain_multipliers():
    """They need the touchdown adjustment, so they must not be in both maps."""
    assert not (set(FIRST_DOWN_KEYS) & set(STAT_KEY_COLUMNS))


# ------------------------------------------------------- the roadmap's gates

def test_half_ppr_and_full_ppr_differ_by_exactly_half_a_point_per_reception(warehouse):
    """The roadmap's stated gate, run over a full week of real stat lines."""
    rows = query(
        "SELECT * FROM player_week_stats WHERE season = ? AND week = 5 "
        "AND season_type = 'REG'",
        [SEASON],
    )
    assert len(rows) > 500

    for row in rows:
        half = score_stat_line(row, HALF_PPR)
        full = score_stat_line(row, FULL_PPR)
        expected = 0.5 * (row["receptions"] or 0)
        assert full - half == pytest.approx(expected, abs=1e-9), row["player_id"]


@pytest.mark.parametrize("name,week,position,recorded", SLEEPER_RECORDED)
def test_matches_points_sleeper_actually_recorded(warehouse, name, week, position, recorded):
    """Spot checks against a real league's recorded scores."""
    stats = _stat_line(name, week)
    assert score_stat_line(stats, WOLFPACK_SCORING, position=position) == pytest.approx(
        recorded, abs=0.05
    )


def test_passing_touchdown_value_is_read_from_the_league(warehouse):
    """Wolfpack pays 4 per passing TD, the other two leagues pay 6. Nothing in
    the engine may assume either."""
    stats = {"passing_tds": 3, "passing_yards": 300}
    four = score_stat_line(stats, {"pass_td": 4.0, "pass_yd": 0.04})
    six = score_stat_line(stats, {"pass_td": 6.0, "pass_yd": 0.04})
    assert six - four == pytest.approx(6.0)


# -------------------------------------------------------------- projections

def test_opponent_adjustment_is_bounded_and_ordered():
    assert opponent_adjustment(1) == pytest.approx(1 - OPPONENT_SWING)
    assert opponent_adjustment(32) == pytest.approx(1 + OPPONENT_SWING)
    assert opponent_adjustment(None) == 1.0
    assert opponent_adjustment(1) < opponent_adjustment(16) < opponent_adjustment(32)


def test_recent_form_moves_the_current_season_signal(linked_leagues):
    """Within a season, a player trending up must project above their flat
    average once the prior-season anchor has faded.

    Asserted on the current-season signal rather than the final number, since
    the final number is deliberately shrunk toward last year — see
    test_projection_leans_on_last_season_early.
    """
    league_id = query("SELECT league_id FROM leagues WHERE format='dynasty' LIMIT 1")[0][
        "league_id"
    ]
    rows = query(
        """
        SELECT s.player_id FROM player_week_stats s
        JOIN players p ON p.player_id=s.player_id AND p.season=s.season
        WHERE s.season=? AND p.position='RB' AND s.season_type='REG'
        GROUP BY s.player_id HAVING COUNT(*) >= 10 LIMIT 40
        """,
        [SEASON],
    )
    projections = [project_player(league_id, r["player_id"], SEASON) for r in rows]
    trending_up = [p for p in projections if p.recent_avg > p.season_avg]
    assert trending_up, "expected at least one improving player"

    for projection in trending_up:
        current_signal = (
            RECENT_WEIGHT * projection.recent_avg
            + SEASON_WEIGHT * projection.season_avg
        )
        assert current_signal > projection.season_avg


def test_projection_of_a_player_with_no_games_is_zero_not_an_error(linked_leagues):
    league_id = query("SELECT league_id FROM leagues LIMIT 1")[0]["league_id"]
    projection = project_player(league_id, "00-0000000", SEASON)
    assert projection.total == 0.0
    assert projection.games_played == 0


def test_unlinked_league_raises_rather_than_scoring_zero(warehouse):
    with pytest.raises(LookupError):
        project_player("not-a-league", "00-0000000", SEASON)


# ----------------------------------------------------------------- scarcity

def test_superflex_demands_far_more_quarterbacks(linked_leagues):
    """A SUPER_FLEX slot is filled by a QB in practice, which is exactly why it
    moves QB valuation more than any other setting."""
    rows = query("SELECT league_id, total_rosters FROM leagues WHERE superflex LIMIT 1")
    if not rows:
        pytest.skip("no superflex league linked")

    demand = starter_demand(rows[0]["league_id"])
    assert demand["QB"] >= 2 * rows[0]["total_rosters"]


def test_replacement_level_is_positive_and_qb_highest_in_superflex(linked_leagues):
    rows = query("SELECT league_id FROM leagues WHERE superflex LIMIT 1")
    if not rows:
        pytest.skip("no superflex league linked")
    league_id = rows[0]["league_id"]

    levels = {
        position: positional_scarcity(league_id, position, SEASON).replacement_points_per_game
        for position in ("QB", "RB", "WR", "TE")
    }
    assert all(v > 0 for v in levels.values()), levels
    assert levels["QB"] > max(levels["RB"], levels["WR"], levels["TE"]), levels


def test_scarcity_for_an_unlinked_league_raises(warehouse):
    with pytest.raises(LookupError):
        positional_scarcity("not-a-league", "RB", SEASON)
