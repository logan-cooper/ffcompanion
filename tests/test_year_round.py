"""Phase 3c gate: the app is useful in February and in week 1, not only in
November.

Two failure modes this locks down:

1. **Offseason dynasty.** A dynasty league rolls to the next season in January
   and trades all spring, but `players` is keyed `(player_id, season)` — so
   before the new season has data, a naive lookup finds nothing and every
   player silently prices at zero.
2. **Early-season redraft.** With one game played, projecting a full season off
   that sample makes a fluke opener look like an elite year.
"""

from __future__ import annotations

import dataclasses

import pytest

from advisor.context import GAMES_PER_SEASON, LeagueContext, load_context, weeks_completed
from advisor.db import query
from advisor.players import age_on_season, latest_season_with_data, player_profile
from advisor.scoring.projections import (
    PRIOR_STRENGTH_GAMES,
    clear_scarcity_cache,
    project_player,
    weekly_points,
)
from advisor.valuation import DynastyValuation, clear_caches

SEASON = 2025
NEXT_SEASON = 2026


@pytest.fixture
def wolfpack(linked_leagues):
    clear_caches()
    clear_scarcity_cache()
    rows = query("SELECT league_id FROM leagues WHERE name = 'Wolfpack Dynasty'")
    if not rows:
        pytest.skip("Wolfpack Dynasty not linked")
    return load_context(rows[0]["league_id"], roster_id=8)


@pytest.fixture
def offseason_ctx(wolfpack):
    """A dynasty league in the offseason: next season, no games played."""
    return dataclasses.replace(
        wolfpack, season=NEXT_SEASON, current_week=0, stats_season=SEASON
    )


# ------------------------------------------------------- player identity

def test_player_resolves_from_an_earlier_season(warehouse):
    """The offseason lookup that used to return nothing."""
    row = query(
        "SELECT player_id FROM players WHERE season = ? AND full_name = ?",
        [SEASON, "Christian McCaffrey"],
    )[0]

    profile = player_profile(row["player_id"], NEXT_SEASON)
    assert profile["full_name"] == "Christian McCaffrey"
    assert profile["position"] == "RB"
    assert profile["source_season"] == SEASON
    assert profile["seasons_stale"] == 1


def test_age_advances_with_the_valuation_season(warehouse):
    """Valuing 2026 off 2025 rows must not reuse 2025 ages — that hands every
    roster a free year of youth, which is backwards for dynasty."""
    row = query(
        "SELECT player_id FROM players WHERE season = ? AND full_name = ?",
        [SEASON, "Christian McCaffrey"],
    )[0]

    this_year = player_profile(row["player_id"], SEASON)["age"]
    next_year = player_profile(row["player_id"], NEXT_SEASON)["age"]
    assert next_year == pytest.approx(this_year + 1.0, abs=0.02)


def test_age_on_season_handles_missing_birth_date():
    assert age_on_season(None, SEASON) is None


def test_unknown_player_does_not_raise(warehouse):
    profile = player_profile("00-0000000", SEASON)
    assert profile["position"] is None
    assert profile["age"] is None


def test_latest_season_with_data_respects_the_bound(warehouse):
    assert latest_season_with_data(NEXT_SEASON) == SEASON
    assert latest_season_with_data(2024) == 2024
    assert latest_season_with_data(1990) is None


# --------------------------------------------------------------- offseason

def test_offseason_context_reports_a_full_season_ahead(offseason_ctx):
    assert offseason_ctx.is_offseason
    assert not offseason_ctx.season_started
    assert offseason_ctx.games_remaining == GAMES_PER_SEASON
    assert offseason_ctx.stats_season == SEASON


def test_offseason_dynasty_values_are_not_all_zero(offseason_ctx):
    """The headline failure: before this, every player priced at zero from
    February to September — exactly when dynasty leagues trade most."""
    dynasty = DynastyValuation()
    rows = query(
        """
        SELECT p.player_id FROM players p
        JOIN v_player_season_totals t ON t.player_id=p.player_id AND t.season=p.season
        WHERE p.season = ? AND p.position IN ('QB','RB','WR','TE') AND t.games >= 12
        ORDER BY t.passing_yards + t.rushing_yards + t.receiving_yards DESC
        LIMIT 20
        """,
        [SEASON],
    )
    values = [dynasty.player_value(r["player_id"], offseason_ctx) for r in rows]

    assert any(v.win_now > 0 for v in values), "no player has win-now value"
    assert any(v.future > 0 for v in values), "no player has future value"
    assert all(v.position is not None for v in values), "identity failed to resolve"
    assert all(v.age is not None for v in values), "ages failed to resolve"


def test_offseason_still_separates_young_from_old(offseason_ctx):
    """Age has to keep working across the season boundary, not just within one."""
    dynasty = DynastyValuation()

    def value(name: str):
        rows = query(
            "SELECT player_id FROM players WHERE season = ? AND full_name = ?",
            [SEASON, name],
        )
        if not rows:
            pytest.skip(f"{name} not in warehouse")
        return dynasty.player_value(rows[0]["player_id"], offseason_ctx)

    young_qb = value("Drake Maye")
    old_qb = value("Aaron Rodgers")

    assert young_qb.future > old_qb.future
    assert young_qb.age < old_qb.age


def test_win_now_is_age_adjusted_across_a_season_boundary(wolfpack, offseason_ctx):
    """A 30-year-old back should not be projected to repeat his age-29 season.

    This is a projection concern, not a dynasty one — it applies in redraft too.
    Compared against the player's own 2025 output rather than against another
    context, since a different context also means a different baseline season.
    """
    rows = query(
        "SELECT player_id FROM players WHERE season = ? AND full_name = ?",
        [SEASON, "Christian McCaffrey"],
    )
    if not rows:
        pytest.skip("player not in warehouse")
    player_id = rows[0]["player_id"]

    actual_2025 = weekly_points(wolfpack.league_id, player_id, SEASON)
    actual_avg = sum(points for _, points in actual_2025) / len(actual_2025)

    projected_2026 = DynastyValuation().player_value(
        player_id, offseason_ctx
    ).points_per_game

    assert projected_2026 < actual_avg, "aging back projected flat into next year"
    # But not annihilated — one year of decline, not a cliff.
    assert projected_2026 > 0.5 * actual_avg


def test_a_prime_age_player_barely_declines_across_a_boundary(offseason_ctx):
    """The same machinery must not punish a 24-year-old for turning 25."""
    rows = query(
        "SELECT player_id FROM players WHERE season = ? AND full_name = ?",
        [SEASON, "Drake Maye"],
    )
    if not rows:
        pytest.skip("player not in warehouse")

    actual = weekly_points(offseason_ctx.league_id, rows[0]["player_id"], SEASON)
    actual_avg = sum(points for _, points in actual) / len(actual)
    projected = DynastyValuation().player_value(
        rows[0]["player_id"], offseason_ctx
    ).points_per_game

    assert projected == pytest.approx(actual_avg, rel=0.1)


# ------------------------------------------------------------ early season

def test_projection_leans_on_last_season_early(wolfpack):
    """Week 1 must not extrapolate a season from one game."""
    rows = query(
        """
        SELECT s.player_id FROM player_week_stats s
        WHERE s.season = ? AND s.season_type='REG' AND s.week = 1
        GROUP BY s.player_id LIMIT 60
        """,
        [SEASON],
    )
    checked = 0
    for row in rows:
        projection = project_player(
            wolfpack.league_id, row["player_id"], SEASON, through_week=1
        )
        if projection.games_played != 1 or projection.prior_source == "replacement":
            continue
        checked += 1
        expected_weight = PRIOR_STRENGTH_GAMES / (1 + PRIOR_STRENGTH_GAMES)
        assert projection.prior_weight == pytest.approx(expected_weight, abs=0.01)
        # The projection must sit between this week's game and last year.
        low = min(projection.season_avg, projection.prior_baseline)
        high = max(projection.season_avg, projection.prior_baseline)
        assert low - 0.01 <= projection.points_per_game <= high + 0.01

    assert checked > 5, "expected several players with prior-season history"


def test_prior_weight_falls_as_the_season_accumulates(wolfpack):
    row = query(
        "SELECT player_id FROM players WHERE season = ? AND full_name = ?",
        [SEASON, "Christian McCaffrey"],
    )[0]

    weights = [
        project_player(
            wolfpack.league_id, row["player_id"], SEASON, through_week=week
        ).prior_weight
        for week in (1, 4, 8, 14)
    ]
    assert weights == sorted(weights, reverse=True)
    assert weights[0] > 0.8  # week 1 is mostly last season
    assert weights[-1] < 0.5  # by week 14 this season dominates


def test_no_games_played_means_pure_prior_baseline(wolfpack):
    row = query(
        "SELECT player_id FROM players WHERE season = ? AND full_name = ?",
        [SEASON, "Christian McCaffrey"],
    )[0]

    projection = project_player(
        wolfpack.league_id, row["player_id"], SEASON, through_week=0
    )
    assert projection.games_played == 0
    assert projection.prior_weight == 1.0
    assert projection.points_per_game == pytest.approx(projection.prior_baseline)


def test_through_week_zero_is_not_treated_as_the_whole_season(wolfpack):
    """`if through_week` would make 0 falsy and silently return every week."""
    row = query(
        "SELECT player_id FROM players WHERE season = ? AND full_name = ?",
        [SEASON, "Christian McCaffrey"],
    )[0]
    assert weekly_points(wolfpack.league_id, row["player_id"], SEASON, through_week=0) == []


def test_players_with_no_history_fall_back_to_replacement(wolfpack):
    """A player with no prior season is a replacement-level unknown, not a zero
    and not whatever a one-game sample claims."""
    projection = project_player(
        wolfpack.league_id, "00-0000000", SEASON, through_week=1
    )
    assert projection.prior_source in {"replacement", "none"}


# --------------------------------------------------------- no regression

def test_late_season_answers_barely_move(wolfpack):
    """The prior anchor must not distort a fully-evidenced season."""
    row = query(
        "SELECT player_id FROM players WHERE season = ? AND full_name = ?",
        [SEASON, "Christian McCaffrey"],
    )[0]

    projection = project_player(
        wolfpack.league_id, row["player_id"], SEASON, through_week=17
    )
    current_signal = 0.6 * projection.recent_avg + 0.4 * projection.season_avg
    assert projection.points_per_game == pytest.approx(current_signal, rel=0.35)


def test_weeks_completed_reads_from_the_data(warehouse):
    assert weeks_completed(SEASON) == 18
    assert weeks_completed(NEXT_SEASON) == 0


def test_context_infers_the_current_week(wolfpack):
    """A completed season should report itself complete without being told."""
    assert wolfpack.current_week == 18
    assert wolfpack.season_complete
    assert not wolfpack.is_offseason


def test_default_context_is_not_assumed_complete():
    ctx = LeagueContext(league_id="L", season=SEASON)
    assert ctx.is_offseason
    assert ctx.games_remaining == GAMES_PER_SEASON
