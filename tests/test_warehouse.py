"""Phase 1 gate: the ingested 2025 warehouse matches reality.

The stat lines below were verified by hand against ESPN's published 2025
regular-season leaderboards, not against the pipeline that loaded them. If a
column mapping regresses, these fail.
"""

from __future__ import annotations

import argparse

import pytest

from advisor.cli import build_parser, season_list
from advisor.db import query
from advisor.warehouse.ingest import _week_filter

SEASON = 2025

# name -> externally verified 2025 regular-season totals.
# Sources: ESPN 2025 passing / rushing / receiving leaders.
HAND_VERIFIED_LINES = {
    "Matthew Stafford": {
        "games": 17,
        "completions": 388,
        "attempts": 597,
        "passing_yards": 4707.0,
        "passing_tds": 46,
        "interceptions": 8,
    },
    "James Cook": {
        "games": 17,
        "carries": 309,
        "rushing_yards": 1621.0,
        "rushing_tds": 12,
    },
    "Jaxon Smith-Njigba": {
        "games": 17,
        "receptions": 119,
        "targets": 163,
        "receiving_yards": 1793.0,
        "receiving_tds": 10,
    },
}

# Draft position and birth date drive dynasty valuation in Phase 3b, so they
# get pinned the same way the production stats do.
HAND_VERIFIED_PROFILES = {
    "Bijan Robinson": {"draft_round": 1, "draft_pick": 8, "rookie_year": 2023},
    "Matthew Stafford": {"draft_round": 1, "draft_pick": 1, "rookie_year": 2009},
    "Jaxon Smith-Njigba": {"draft_round": 1, "draft_pick": 20, "rookie_year": 2023},
}


def _season_totals(name: str) -> dict:
    rows = query(
        """
        SELECT t.*
        FROM v_player_season_totals t
        JOIN players p ON p.player_id = t.player_id AND p.season = t.season
        WHERE t.season = ? AND p.full_name = ?
        """,
        [SEASON, name],
    )
    assert len(rows) == 1, f"expected exactly one {name} in {SEASON}, got {len(rows)}"
    return rows[0]


@pytest.mark.parametrize("name,expected", HAND_VERIFIED_LINES.items())
def test_hand_verified_stat_lines_match_exactly(warehouse, name, expected):
    actual = _season_totals(name)
    assert {k: actual[k] for k in expected} == expected


@pytest.mark.parametrize("name,expected", HAND_VERIFIED_PROFILES.items())
def test_hand_verified_draft_profiles(warehouse, name, expected):
    rows = query(
        "SELECT * FROM players WHERE season = ? AND full_name = ?", [SEASON, name]
    )
    assert len(rows) == 1
    assert {k: rows[0][k] for k in expected} == expected


def test_no_fantasy_points_column_anywhere(warehouse):
    """Points are league-specific and computed at query time (Phase 3).

    nflverse publishes fantasy_points columns; storing one would silently make
    every league's numbers wrong for anyone who joined against it.
    """
    columns = query(
        """
        SELECT table_name, column_name FROM information_schema.columns
        WHERE lower(column_name) LIKE '%fantasy%'
        """
    )
    assert columns == []


def test_dynasty_fields_are_populated_for_regulars(warehouse):
    """Age is impossible to backfill without re-ingesting, so it must land now."""
    row = query(
        """
        SELECT COUNT(*) AS n, SUM(CASE WHEN age IS NULL THEN 1 ELSE 0 END) AS null_age
        FROM players p
        WHERE p.season = ? AND p.position IN ('QB','RB','WR','TE')
          AND p.player_id IN (
              SELECT player_id FROM v_player_season_totals
              WHERE season = ? AND games >= 8
          )
        """,
        [SEASON, SEASON],
    )[0]
    assert row["n"] > 200
    assert row["null_age"] == 0


def test_sleeper_crosswalk_covers_real_contributors(warehouse):
    """The Sleeper id is how Phase 2 joins a league roster to these stats.

    Deep practice-squad players legitimately have no Sleeper id, so the bar is
    production rather than games played — a special-teamer active for 11 weeks
    with 15 receiving yards is not someone a league will ever roster.
    """
    missing = query(
        """
        SELECT p.full_name
        FROM players p
        JOIN v_player_season_totals t
          ON t.player_id = p.player_id AND t.season = p.season
        WHERE p.season = ? AND p.sleeper_id IS NULL
          AND p.position IN ('QB','RB','WR','TE')
          AND COALESCE(t.passing_yards, 0)
              + COALESCE(t.rushing_yards, 0)
              + COALESCE(t.receiving_yards, 0) >= 100
        """,
        [SEASON],
    )
    assert missing == []


def test_weekly_tables_have_no_duplicate_rows(warehouse):
    """Idempotency guard: re-ingesting must replace weeks, not append them."""
    for table in ("player_week_stats", "player_week_usage"):
        dupes = query(
            f"""
            SELECT player_id, week, season_type, COUNT(*) AS n
            FROM {table} WHERE season = ?
            GROUP BY player_id, week, season_type HAVING COUNT(*) > 1
            """,
            [SEASON],
        )
        assert dupes == [], f"{table} has duplicate rows"


def test_regular_season_is_complete(warehouse):
    weeks = query(
        "SELECT DISTINCT week FROM player_week_stats "
        "WHERE season = ? AND season_type = 'REG' ORDER BY week",
        [SEASON],
    )
    assert [w["week"] for w in weeks] == list(range(1, 19))


def test_rolling_3wk_window_never_exceeds_three_games(warehouse):
    row = query(
        "SELECT MAX(games_in_window) AS mx FROM v_player_rolling_3wk WHERE season = ?",
        [SEASON],
    )[0]
    assert row["mx"] == 3


def test_rolling_average_matches_manual_calculation(warehouse):
    """Spot-check the window function against the three weeks it should cover."""
    player = query(
        """
        SELECT player_id FROM v_player_season_totals
        WHERE season = ? AND games = 17 ORDER BY rushing_yards DESC LIMIT 1
        """,
        [SEASON],
    )[0]["player_id"]

    rolling = query(
        "SELECT avg_rushing_yards FROM v_player_rolling_3wk "
        "WHERE player_id = ? AND season = ? AND week = 5",
        [player, SEASON],
    )[0]["avg_rushing_yards"]

    manual = query(
        "SELECT AVG(rushing_yards) AS a FROM player_week_stats "
        "WHERE player_id = ? AND season = ? AND season_type = 'REG' "
        "AND week BETWEEN 3 AND 5",
        [player, SEASON],
    )[0]["a"]

    assert rolling == pytest.approx(manual)


def test_defense_rank_is_dense_over_all_teams(warehouse):
    row = query(
        """
        SELECT COUNT(*) AS teams, MIN(defense_rank) AS lo, MAX(defense_rank) AS hi
        FROM v_position_defense_rank WHERE season = ? AND position = 'RB'
        """,
        [SEASON],
    )[0]
    assert row["teams"] == 32
    assert (row["lo"], row["hi"]) == (1, 32)


def test_red_zone_touches_is_the_sum_of_its_parts(warehouse):
    bad = query(
        "SELECT COUNT(*) AS n FROM player_week_usage "
        "WHERE season = ? AND red_zone_touches "
        "        <> red_zone_carries + red_zone_targets",
        [SEASON],
    )[0]["n"]
    assert bad == 0


def test_snap_share_is_a_fraction(warehouse):
    row = query(
        "SELECT MIN(snap_share) AS lo, MAX(snap_share) AS hi "
        "FROM player_week_usage WHERE season = ? AND snap_share IS NOT NULL",
        [SEASON],
    )[0]
    assert 0.0 <= row["lo"] <= row["hi"] <= 1.0


def test_ingest_log_records_every_table(warehouse):
    sources = {
        r["source"]
        for r in query("SELECT DISTINCT source FROM ingest_log WHERE season = ?", [SEASON])
    }
    assert sources == {"players", "player_week_stats", "player_week_usage", "schedules"}


def test_schedules_cover_the_full_regular_season(warehouse):
    row = query(
        "SELECT COUNT(*) AS n FROM schedules WHERE season = ? AND game_type = 'REG'",
        [SEASON],
    )[0]
    assert row["n"] == 272  # 32 teams x 17 games / 2


@pytest.mark.parametrize(
    "weeks,expected_sql,expected_params",
    [
        (None, "", []),
        ([], "", []),
        ([4], " AND week IN (?)", [4]),
        ([1, 2, 3], " AND week IN (?, ?, ?)", [1, 2, 3]),
    ],
)
def test_week_filter_builds_parameterized_sql(weeks, expected_sql, expected_params):
    """Week lists are bound, never interpolated."""
    assert _week_filter(weeks) == (expected_sql, expected_params)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2025", [2025]),
        ("2023,2024,2025", [2023, 2024, 2025]),
        (" 2023 , 2024 ", [2023, 2024]),
        ("2024,2024", [2024]),  # repeats collapse rather than ingesting twice
    ],
)
def test_season_list_parses(raw, expected):
    assert season_list(raw) == expected


@pytest.mark.parametrize("raw", ["", "not-a-year", "1998", "2025,abc", ","])
def test_season_list_rejects_junk(raw):
    with pytest.raises(argparse.ArgumentTypeError):
        season_list(raw)


def test_ingest_accepts_multiple_seasons():
    args = build_parser().parse_args(
        ["ingest", "--season", "2023,2024", "--season", "2025"]
    )
    assert args.seasons == [2023, 2024, 2025]


def test_rolling_window_never_spans_a_season_boundary(warehouse):
    """Week 1 must not average in the previous season's final games.

    The view partitions by (player_id, season); if that ever regressed, a
    player's week-1 line would silently blend last year's production.
    """
    leaked = query(
        """
        SELECT COUNT(*) AS n FROM v_player_rolling_3wk r
        WHERE r.week = 1 AND r.games_in_window > 1
        """
    )[0]["n"]
    assert leaked == 0


def test_season_totals_are_isolated_per_season(warehouse):
    """A player present in several seasons gets one row per season, not a blend."""
    rows = query(
        """
        SELECT t.season, t.games FROM v_player_season_totals t
        JOIN players p ON p.player_id = t.player_id AND p.season = t.season
        WHERE p.full_name = 'Malik Nabers' ORDER BY t.season
        """
    )
    if len(rows) < 2:
        pytest.skip("only one season ingested; run: make warehouse")
    assert len({r["season"] for r in rows}) == len(rows)
