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


# ------------------------------------------------------------- listing leagues
#
# `list_leagues` decides which league every answer is about, so these run the
# real SQL against the real schema. Two Phase 8 bugs were invented identifiers
# (a `rosters` table, a `rows` column) that only executing the query catches.

def _link(league_id: str, name: str, fmt: str, season: int = 2025) -> None:
    query(
        """
        INSERT INTO leagues (league_id, season, name, status, total_rosters,
                             sleeper_type, format, format_source, superflex,
                             has_taxi, is_continuation, roster_positions,
                             scoring_settings, settings, fetched_at)
        VALUES (?, ?, ?, 'complete', 12, 2, ?, 'settings.type', false,
                false, false, '[]', '{}', '{}', now())
        """,
        [league_id, season, name, fmt],
    )


def _own(league_id: str, roster_id: int, user_id: str, display_name: str) -> None:
    query(
        "INSERT INTO league_users (league_id, user_id, display_name, team_name) "
        "VALUES (?, ?, ?, NULL)",
        [league_id, user_id, display_name],
    )
    query(
        "INSERT INTO league_rosters (league_id, roster_id, owner_id, players, "
        "starters, taxi, reserve, wins, losses, ties, fpts) "
        "VALUES (?, ?, ?, '[]', '[]', '[]', '[]', 0, 0, 0, 0.0)",
        [league_id, roster_id, user_id],
    )


def _intend(league_id: str, roster_id: int, intent: str = "contend") -> None:
    query(
        "INSERT INTO team_intent (league_id, roster_id, intent, updated_at) "
        "VALUES (?, ?, ?, now())",
        [league_id, roster_id, intent],
    )


@pytest.fixture
def as_user(monkeypatch):
    """Configure a Sleeper username, which is what identifies the user's roster."""
    from advisor.config import get_settings

    monkeypatch.setattr(get_settings(), "sleeper_username", "tester", raising=False)


def test_listing_names_the_roster_the_user_owns(league_db, as_user):
    """Nothing in the league data marks which manager is you, so it comes from
    the configured username joined through league_rosters.owner_id."""
    from advisor.context import list_leagues

    _link("L1", "Dynasty A", "dynasty")
    _own("L1", 7, "U-tester", "tester")
    _own("L1", 3, "U-other", "someone-else")

    row = list_leagues()[0]
    assert row["owned_roster_id"] == 7
    assert row["roster_id"] == 7


def test_team_intent_outranks_sleeper_ownership(league_db, as_user):
    """Intent is user-set and deliberate; ownership is merely a fact about the
    account. The stated one wins."""
    from advisor.context import list_leagues

    _link("L1", "Dynasty A", "dynasty")
    _own("L1", 7, "U-tester", "tester")
    _intend("L1", 4)

    row = list_leagues()[0]
    assert row["intent_roster_id"] == 4
    assert row["owned_roster_id"] == 7
    assert row["roster_id"] == 4, "a stated intent must win"


def test_a_league_with_intent_on_two_rosters_is_listed_once(league_db, as_user):
    """team_intent is keyed (league_id, roster_id), so a plain join fans one
    league into several rows and a LIMIT 1 over that picks arbitrarily."""
    from advisor.context import list_leagues

    _link("L1", "Dynasty A", "dynasty")
    _intend("L1", 4)
    _intend("L1", 9)

    rows = list_leagues()
    assert len(rows) == 1
    assert rows[0]["roster_id"] == 4, "lowest roster, deterministically"


def test_survival_is_not_the_first_league_offered(league_db, as_user):
    """A survival league has no persistent rosters, so landing a newcomer there
    answers most questions with "that does not apply here"."""
    from advisor.context import list_leagues

    _link("S1", "AAA survival", "survival")   # alphabetically first
    _link("D1", "ZZZ dynasty", "dynasty")

    assert list_leagues()[0]["league_id"] == "D1"


def test_listing_works_with_no_username_configured(league_db, monkeypatch):
    """The fresh-install state: linked leagues, nothing identifying the user."""
    from advisor.config import get_settings
    from advisor.context import list_leagues

    monkeypatch.setattr(get_settings(), "sleeper_username", None, raising=False)
    _link("L1", "Dynasty A", "dynasty")
    _own("L1", 7, "U-tester", "tester")

    row = list_leagues()[0]
    assert row["roster_id"] is None, "cannot know which roster without a username"


# ------------------------------------------------------- who "you" are

def test_an_account_set_in_the_app_outranks_the_env_username(league_db, as_user):
    """`as_user` configures SLEEPER_USERNAME, so this asserts the precedence.

    Someone typing their name into the UI is the more recent and more explicit
    statement, and it is the only one of the two that takes effect without a
    restart — `get_settings()` is lru_cached.
    """
    from advisor.context import list_leagues
    from advisor.warehouse.account import set_account

    _link("L1", "Dynasty A", "dynasty")
    _own("L1", 7, "U-tester", "tester")
    _own("L1", 3, "U-typed", "typed-in-the-app")

    set_account("typed-in-the-app", "U-typed")

    assert list_leagues()[0]["roster_id"] == 3


def test_ownership_survives_a_rename_on_sleeper(league_db, as_user):
    """A user id is stable; a display name is not. Matching on the id is what
    keeps a linked league pointing at the right roster after a rename."""
    from advisor.context import list_leagues
    from advisor.warehouse.account import set_account

    _link("L1", "Dynasty A", "dynasty")
    _own("L1", 7, "U-tester", "the-old-name")

    set_account("a-completely-new-name", "U-tester")

    assert list_leagues()[0]["owned_roster_id"] == 7


def test_an_account_replaces_rather_than_accumulates(league_db):
    """One manager, one row. Two accounts would make "your roster" ambiguous
    and the resolution order arbitrary."""
    from advisor.warehouse.account import get_account, set_account

    set_account("first", "U-1")
    set_account("second", "U-2")

    assert query("SELECT COUNT(*) AS n FROM sleeper_account")[0]["n"] == 1
    assert get_account()["username"] == "second"


def test_listing_leagues_works_on_a_warehouse_built_before_accounts_existed(
    league_db, as_user
):
    """Every warehouse predating this feature has no `sleeper_account` table,
    and `list_leagues` reads it on every lookup — so a missing table would break
    both interfaces at once, not just the new panel."""
    from advisor.context import list_leagues

    _link("L1", "Dynasty A", "dynasty")
    _own("L1", 7, "U-tester", "tester")
    query("DROP TABLE IF EXISTS sleeper_account")

    assert list_leagues()[0]["roster_id"] == 7


def test_an_unlinked_league_id_is_refused(league_db, as_user):
    """Unvalidated, a bad id reached the web layer's generator and killed the
    SSE stream after a 200 had already been sent."""
    from advisor.cli import _pick_league

    _link("L1", "Dynasty A", "dynasty")

    with pytest.raises(LookupError, match="not linked"):
        _pick_league("no-such-league")
