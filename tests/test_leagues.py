"""League ingestion: storage, the derived waiver universe, and team intent.

These run offline against fixtures. The tests that need real linked leagues skip
unless `make link-league` has been run.
"""

from __future__ import annotations

import json

import pytest

from advisor.db import query
from advisor.league_format import DEFAULT_TEAM_INTENT, detect_format
from advisor.warehouse.leagues import (
    _load_available_players,
    _load_league,
    _load_rosters,
    _load_traded_picks,
    get_team_intent,
    set_team_intent,
)
from advisor.warehouse.schema import create_schema
from tests.fixtures import leagues as fx

POOL = {
    "1": {"full_name": "Rostered Starter", "position": "RB", "team": "BUF", "gsis_id": "00-1"},
    "2": {"full_name": "Taxi Squad Guy", "position": "WR", "team": "SEA", "gsis_id": "00-2"},
    "3": {"full_name": "Injured Reserve", "position": "TE", "team": "KC", "gsis_id": "00-3"},
    "4": {"full_name": "Free Agent", "position": "QB", "team": "NYJ", "gsis_id": "00-4"},
    "5": {"full_name": "Also Available", "position": "WR", "team": "LA", "gsis_id": None},
}

ROSTERS = [
    {
        "roster_id": 1,
        "owner_id": "user-a",
        "players": ["1"],
        "starters": ["1"],
        "taxi": ["2"],
        "reserve": ["3"],
        "settings": {"wins": 9, "losses": 5, "ties": 0, "fpts": 1500.5},
    }
]


@pytest.fixture
def league_db(temp_db):
    create_schema()
    return temp_db


def test_scoring_settings_are_stored_verbatim(league_db):
    """Phase 3 reads a league's own rules; normalising them here would mean
    guessing which keys matter before we know."""
    _load_league(fx.DYNASTY_SUPERFLEX, 2025, detect_format(fx.DYNASTY_SUPERFLEX))

    row = query("SELECT * FROM leagues WHERE league_id = ?", [fx.DYNASTY_SUPERFLEX["league_id"]])[0]
    assert json.loads(row["scoring_settings"]) == fx.DYNASTY_SUPERFLEX["scoring_settings"]
    assert json.loads(row["roster_positions"]) == fx.DYNASTY_SUPERFLEX["roster_positions"]


def test_league_row_records_format_and_audit_fields(league_db):
    _load_league(fx.DYNASTY_SUPERFLEX, 2025, detect_format(fx.DYNASTY_SUPERFLEX))

    row = query("SELECT * FROM leagues")[0]
    assert row["format"] == "dynasty"
    assert row["sleeper_type"] == 2
    assert row["superflex"] is True
    assert row["has_taxi"] is True
    assert row["is_continuation"] is True
    assert row["format_source"]


def test_relinking_a_league_replaces_rather_than_duplicates(league_db):
    detection = detect_format(fx.DYNASTY_SUPERFLEX)
    for _ in range(3):
        _load_league(fx.DYNASTY_SUPERFLEX, 2025, detection)
        _load_rosters(fx.DYNASTY_SUPERFLEX["league_id"], ROSTERS)

    assert query("SELECT COUNT(*) AS n FROM leagues")[0]["n"] == 1
    assert query("SELECT COUNT(*) AS n FROM league_rosters")[0]["n"] == 1


def test_available_players_excludes_taxi_and_reserve(league_db):
    """A taxi-squad or IR player is rostered, not a free agent. Missing this
    would offer to add players someone already owns."""
    n = _load_available_players("L1", ROSTERS, POOL)

    available = {
        r["sleeper_id"] for r in query("SELECT sleeper_id FROM available_players")
    }
    assert n == 2
    assert available == {"4", "5"}


def test_available_players_is_derived_per_league(league_db):
    """The same player can be a free agent in one league and rostered in another."""
    _load_available_players("L1", ROSTERS, POOL)
    _load_available_players("L2", [], POOL)

    l1 = {r["sleeper_id"] for r in query("SELECT sleeper_id FROM available_players WHERE league_id='L1'")}
    l2 = {r["sleeper_id"] for r in query("SELECT sleeper_id FROM available_players WHERE league_id='L2'")}
    assert l1 == {"4", "5"}
    assert l2 == set(POOL)


def test_available_players_carries_the_gsis_crosswalk(league_db):
    """Without it, a free agent can't be joined to the stats warehouse."""
    _load_available_players("L1", ROSTERS, POOL)
    row = query("SELECT * FROM available_players WHERE sleeper_id = '4'")[0]
    assert row["player_id"] == "00-4"
    assert row["full_name"] == "Free Agent"


def test_available_players_backfills_gsis_from_the_stats_warehouse(league_db):
    """Sleeper's own gsis_id is null for many real contributors, so the
    crosswalk is filled from the other direction too (nflverse publishes
    sleeper_id). A free agent that can't be joined to stats is invisible to any
    tool that ranks the waiver wire by production.
    """
    query(
        "INSERT INTO players (player_id, season, sleeper_id, full_name, position) "
        "VALUES (?, ?, ?, ?, ?)",
        ["00-9999", 2025, "5", "Also Available", "WR"],
    )

    _load_available_players("L1", ROSTERS, POOL, 2025)

    # "5" has no gsis_id in the Sleeper pool but does in the warehouse.
    assert query("SELECT player_id FROM available_players WHERE sleeper_id='5'")[0][
        "player_id"
    ] == "00-9999"
    # "4" already had one from Sleeper and must be left alone.
    assert query("SELECT player_id FROM available_players WHERE sleeper_id='4'")[0][
        "player_id"
    ] == "00-4"


def test_available_players_backfill_is_skipped_without_a_season(league_db):
    _load_available_players("L1", ROSTERS, POOL)
    assert query("SELECT player_id FROM available_players WHERE sleeper_id='5'")[0][
        "player_id"
    ] is None


def test_traded_picks_keep_one_row_per_pick(league_db):
    """Sleeper lists a pick once per trade in its chain; only the current
    holder matters, and the composite key would otherwise collide."""
    picks = [
        {"season": "2026", "round": 1, "roster_id": 3, "owner_id": 5, "previous_owner_id": 4},
        {"season": "2026", "round": 1, "roster_id": 3, "owner_id": 4, "previous_owner_id": 3},
        {"season": "2027", "round": 2, "roster_id": 1, "owner_id": 2, "previous_owner_id": 1},
    ]
    assert _load_traded_picks("L1", picks) == 2

    rows = query("SELECT * FROM traded_picks ORDER BY season, round")
    assert rows[0]["owner_roster_id"] == 5  # first entry wins
    assert len(rows) == 2


def test_traded_picks_skip_malformed_rows(league_db):
    picks = [
        {"season": "not-a-year", "round": 1, "roster_id": 1},
        {"round": 1, "roster_id": 1},
        {"season": "2027", "round": 3, "roster_id": 2, "owner_id": 9},
    ]
    assert _load_traded_picks("L1", picks) == 1


def test_team_intent_defaults_to_balanced(league_db):
    assert get_team_intent("L1", 1) == DEFAULT_TEAM_INTENT


def test_team_intent_round_trips_and_updates(league_db):
    set_team_intent("L1", 1, "contend")
    assert get_team_intent("L1", 1) == "contend"

    set_team_intent("L1", 1, "rebuild")
    assert get_team_intent("L1", 1) == "rebuild"
    assert query("SELECT COUNT(*) AS n FROM team_intent")[0]["n"] == 1


def test_team_intent_rejects_invalid_values(league_db):
    with pytest.raises(ValueError):
        set_team_intent("L1", 1, "tanking")


def test_relinking_preserves_user_set_intent(league_db):
    """Intent is user data, not fetched data. Re-linking must not wipe it —
    that is why it lives in its own table."""
    detection = detect_format(fx.DYNASTY_SUPERFLEX)
    league_id = fx.DYNASTY_SUPERFLEX["league_id"]

    _load_league(fx.DYNASTY_SUPERFLEX, 2025, detection)
    set_team_intent(league_id, 8, "rebuild")

    _load_league(fx.DYNASTY_SUPERFLEX, 2025, detection)
    _load_rosters(league_id, ROSTERS)
    _load_available_players(league_id, ROSTERS, POOL)

    assert get_team_intent(league_id, 8) == "rebuild"


# ------------------------------------------------------------- live-data gate

def test_linked_leagues_have_distinct_formats(linked_leagues):
    """Phase 2 gate against real data: the detector must not collapse every
    league into one format."""
    rows = query("SELECT name, format, superflex, sleeper_type FROM leagues")
    formats = {r["format"] for r in rows}

    assert len(rows) >= 2
    assert len(formats) >= 2, f"all linked leagues detected as one format: {rows}"
    assert "unknown" not in formats, f"unresolved format in {rows}"


def test_dynasty_league_has_traded_picks(linked_leagues):
    """Picks are dynasty-critical assets; a real dynasty league will have some."""
    rows = query(
        """
        SELECT l.name, COUNT(p.round) AS picks
        FROM leagues l LEFT JOIN traded_picks p ON p.league_id = l.league_id
        WHERE l.format = 'dynasty' GROUP BY l.name
        """
    )
    if not rows:
        pytest.skip("no dynasty league linked")
    assert any(r["picks"] > 0 for r in rows)


def test_productive_free_agents_can_be_joined_to_stats(linked_leagues):
    """The waiver wire is useless if its players can't reach the stats tables."""
    rows = query("SELECT league_id FROM leagues WHERE format = 'dynasty' LIMIT 1")
    if not rows:
        pytest.skip("no dynasty league linked")
    league_id = rows[0]["league_id"]

    unjoinable = query(
        """
        WITH rostered AS (
            SELECT UNNEST(CAST(players AS VARCHAR[])) AS sleeper_id
            FROM league_rosters WHERE league_id = ?
        ),
        producers AS (
            SELECT p.sleeper_id, p.full_name
            FROM players p
            JOIN v_player_season_totals t
              ON t.player_id = p.player_id AND t.season = p.season
            WHERE p.season = 2025 AND p.sleeper_id IS NOT NULL
              AND p.position IN ('QB','RB','WR','TE')
              AND COALESCE(t.passing_yards,0) + COALESCE(t.rushing_yards,0)
                  + COALESCE(t.receiving_yards,0) >= 300
        )
        SELECT pr.full_name FROM producers pr
        LEFT JOIN rostered ro ON ro.sleeper_id = pr.sleeper_id
        LEFT JOIN available_players a
          ON a.sleeper_id = pr.sleeper_id AND a.league_id = ?
        WHERE ro.sleeper_id IS NULL
          AND (a.sleeper_id IS NULL OR a.player_id IS NULL)
        """,
        [league_id, league_id],
    )
    assert unjoinable == []


def test_every_linked_league_has_a_waiver_universe(linked_leagues):
    rows = query(
        """
        SELECT l.league_id, l.name, COUNT(a.sleeper_id) AS available
        FROM leagues l LEFT JOIN available_players a ON a.league_id = l.league_id
        GROUP BY l.league_id, l.name
        """
    )
    assert rows
    for r in rows:
        assert r["available"] > 0, f"{r['name']} has no free agents"
