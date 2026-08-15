"""Keeping rosters and the waiver wire current.

This runs on every page load, so the properties that matter are about *cost* and
*safety* as much as correctness: it must not rewrite the warehouse when nothing
changed, must not leave it damaged when Sleeper is down, and must never take the
app with it. All of that is testable without a network.
"""

from __future__ import annotations

import json

import pytest

from advisor.db import query
from advisor.warehouse import refresh
from advisor.warehouse.schema import create_schema

LEAGUE = {
    "league_id": "L1",
    "name": "Fixture Dynasty",
    "status": "in_season",
    "total_rosters": 2,
    "previous_league_id": None,
    "roster_positions": ["QB", "RB", "WR", "BN"],
    "scoring_settings": {"rec": 1.0},
    "settings": {"type": 2, "taxi_slots": 4},
}

USERS = [
    {"user_id": "U1", "display_name": "tester", "metadata": {"team_name": "Mine"}},
    {"user_id": "U2", "display_name": "rival", "metadata": {}},
]

ROSTERS = [
    {"roster_id": 1, "owner_id": "U1", "players": ["p1", "p2"],
     "starters": ["p1"], "taxi": [], "reserve": [],
     "settings": {"wins": 3, "losses": 1, "ties": 0, "fpts": 400.0}},
    {"roster_id": 2, "owner_id": "U2", "players": ["p3"],
     "starters": ["p3"], "taxi": [], "reserve": [],
     "settings": {"wins": 1, "losses": 3, "ties": 0, "fpts": 300.0}},
]

POOL = {
    "p1": {"full_name": "Rostered One", "position": "RB", "team": "BUF",
           "active": True, "gsis_id": "00-1"},
    "p2": {"full_name": "Rostered Two", "position": "WR", "team": "SEA",
           "active": True, "gsis_id": "00-2"},
    "p3": {"full_name": "Rostered Three", "position": "QB", "team": "KC",
           "active": True, "gsis_id": "00-3"},
    "p4": {"full_name": "Free Agent", "position": "TE", "team": "LV",
           "active": True, "gsis_id": "00-4"},
    "p5": {"full_name": "Also Free", "position": "WR", "team": "NYJ",
           "active": True, "gsis_id": "00-5"},
}


class FakeSleeper:
    """Sleeper, under our control, counting what was asked of it."""

    def __init__(self):
        self.league = json.loads(json.dumps(LEAGUE))
        self.users = json.loads(json.dumps(USERS))
        self.rosters = json.loads(json.dumps(ROSTERS))
        self.picks = []
        self.calls = 0
        self.fail = None

    def install(self, monkeypatch):
        from advisor.sources import sleeper

        def counted(value):
            def call(*_args, **_kwargs):
                self.calls += 1
                if self.fail:
                    raise self.fail
                return value() if callable(value) else value
            return call

        monkeypatch.setattr(sleeper, "get_league", counted(lambda: self.league))
        monkeypatch.setattr(sleeper, "get_league_users", counted(lambda: self.users))
        monkeypatch.setattr(sleeper, "get_rosters", counted(lambda: self.rosters))
        monkeypatch.setattr(sleeper, "get_traded_picks", counted(lambda: self.picks))
        monkeypatch.setattr(sleeper, "get_all_players", lambda refresh=False: POOL)
        return self


@pytest.fixture
def league_db(temp_db, monkeypatch):
    """A linked league and a Sleeper that answers from memory."""
    create_schema()
    query(
        """
        INSERT INTO leagues (league_id, season, name, status, total_rosters,
                             sleeper_type, format, format_source, superflex,
                             has_taxi, is_continuation, roster_positions,
                             scoring_settings, settings, fetched_at)
        VALUES ('L1', 2025, 'Fixture Dynasty', 'in_season', 2, 2, 'dynasty',
                'settings.type', false, true, false, '[]', '{}', '{}', now())
        """
    )
    # The share window hands a just-computed result to the next caller, which is
    # right in a browser and wrong in a test that refreshes twice on purpose.
    refresh._recent.clear()
    return FakeSleeper().install(monkeypatch)


# ---------------------------------------------------------------- the happy path

def test_a_refresh_loads_rosters_and_the_free_agent_pool(league_db):
    result = refresh.refresh_league("L1")

    assert result.ok and result.changed
    assert result.rosters == 2
    assert result.synced_at

    assert query("SELECT COUNT(*) AS n FROM league_rosters")[0]["n"] == 2
    # The wire is the pool minus everyone rostered, derived per league.
    wire = {r["full_name"] for r in query("SELECT full_name FROM available_players")}
    assert wire == {"Free Agent", "Also Free"}


def test_a_dropped_player_appears_on_the_wire(league_db):
    """The reason this runs on every load. Advice about a roster the user no
    longer has is worse than a slow page."""
    refresh.refresh_league("L1")

    league_db.rosters[0]["players"] = ["p1"]  # p2 was dropped
    refresh._recent.clear()
    result = refresh.refresh_league("L1")

    assert result.changed
    wire = {r["full_name"] for r in query("SELECT full_name FROM available_players")}
    assert "Rostered Two" in wire


def test_an_added_player_leaves_the_wire(league_db):
    refresh.refresh_league("L1")

    league_db.rosters[1]["players"] = ["p3", "p4"]  # p4 was claimed
    refresh._recent.clear()
    refresh.refresh_league("L1")

    wire = {r["full_name"] for r in query("SELECT full_name FROM available_players")}
    assert "Free Agent" not in wire


# -------------------------------------------------------------------- the cost

def test_an_unchanged_league_is_not_rewritten(league_db):
    """Most refreshes are no-ops. Rewriting anyway would spend the time this is
    supposed to save and blank `available_players` under whoever is reading."""
    refresh.refresh_league("L1")
    before = query("SELECT fetched_at FROM leagues WHERE league_id = 'L1'")[0]

    refresh._recent.clear()
    result = refresh.refresh_league("L1")

    assert result.ok
    assert result.changed is False
    after = query("SELECT fetched_at FROM leagues WHERE league_id = 'L1'")[0]
    assert after["fetched_at"] == before["fetched_at"], "nothing should have been written"


def test_a_volatile_field_is_not_mistaken_for_a_change(league_db):
    """The fingerprint covers what we would STORE. Hashing the raw response
    would make any field Sleeper touches — a read marker, a counter — look like
    a trade, and then every refresh rewrites everything."""
    refresh.refresh_league("L1")

    league_db.league["last_message_id"] = "something-new"
    league_db.league["last_read_id"] = "also-new"
    league_db.users[0]["metadata"]["avatar"] = "changed"
    refresh._recent.clear()

    assert refresh.refresh_league("L1").changed is False


def test_a_half_written_warehouse_is_repaired_rather_than_skipped(league_db):
    """The fingerprint says "a rewrite would be a no-op", which is only true if
    the last write finished. Left alone, an ingest interrupted mid-way leaves an
    empty wire that every later refresh politely skips."""
    refresh.refresh_league("L1")
    query("DELETE FROM available_players WHERE league_id = 'L1'")
    refresh._recent.clear()

    result = refresh.refresh_league("L1")

    assert result.changed is True, "an empty wire is not an unchanged league"
    assert query("SELECT COUNT(*) AS n FROM available_players")[0]["n"] == 2


def test_a_repeat_within_the_share_window_does_not_ask_sleeper_again(league_db):
    """Two tabs opening together is one burst, not two."""
    refresh.refresh_league("L1")
    after_first = league_db.calls

    refresh.refresh_league("L1")

    assert league_db.calls == after_first, "the second caller reused the result"


def test_force_refetches_even_inside_the_window(league_db):
    refresh.refresh_league("L1")
    after_first = league_db.calls

    refresh.refresh_league("L1", force=True)

    assert league_db.calls > after_first


def test_the_write_path_does_not_refetch_what_it_already_has(league_db):
    """`ingest_league` fetches users, rosters and picks itself unless given
    them. Left to do so, a changed league costs seven requests where four
    would do."""
    refresh.refresh_league("L1")

    assert league_db.calls == 4, f"expected 4 Sleeper calls, got {league_db.calls}"


# ------------------------------------------------------------------- the risks

def test_sleeper_being_down_leaves_the_warehouse_intact(league_db):
    """Local-first. An unreachable Sleeper means older data, not no data."""
    refresh.refresh_league("L1")
    before = query("SELECT * FROM league_rosters ORDER BY roster_id")

    from advisor.sources.sleeper import SleeperError

    league_db.fail = SleeperError("no network")
    refresh._recent.clear()
    result = refresh.refresh_league("L1")

    assert result.ok is False
    assert "unreachable" in result.reason
    assert query("SELECT * FROM league_rosters ORDER BY roster_id") == before


def test_a_league_sleeper_no_longer_returns_is_not_deleted(league_db):
    """Sleeper answers `null` rather than 404. Believing it would wipe a
    working league and write nothing back."""
    refresh.refresh_league("L1")

    league_db.league = None
    refresh._recent.clear()
    result = refresh.refresh_league("L1")

    assert result.ok is False
    assert query("SELECT COUNT(*) AS n FROM league_rosters")[0]["n"] == 2


def test_a_refresh_never_raises(league_db):
    """It runs on every page load. Anything that escapes here takes the whole
    app down over a network blip."""
    from advisor.sources.sleeper import SleeperError

    league_db.fail = SleeperError("boom")

    assert refresh.refresh_league("L1").ok is False
    assert refresh.refresh_league("unknown-league").ok is False


def test_user_set_data_survives_a_refresh(league_db):
    """team_intent and chat history are the user's, not Sleeper's. A refresh
    that ran on every page load and wiped them would be a data-loss bug that
    fires constantly."""
    from advisor.warehouse.conversations import create, history
    from advisor.warehouse.leagues import get_team_intent, set_team_intent

    refresh.refresh_league("L1")
    set_team_intent("L1", 1, "rebuild")
    conversation = create("L1", 1)
    from advisor.warehouse.conversations import append

    append(conversation, "user", "will this survive?")

    league_db.rosters[0]["players"] = ["p1"]
    refresh._recent.clear()
    assert refresh.refresh_league("L1").changed is True

    assert get_team_intent("L1", 1) == "rebuild"
    assert len(history(conversation)) == 1


def test_concurrent_refreshes_do_not_both_fetch(league_db):
    """Two panels and a chat turn can land at once. Without the lock they race
    on the same delete-and-rewrite."""
    import threading

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(refresh.refresh_league("L1")))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(r.ok for r in results)
    assert league_db.calls == 4, "one fetch, shared four ways"


# ------------------------------------------------------------ knowing everyone

def test_the_mirror_covers_players_no_nfl_roster_lists(league_db, monkeypatch):
    """The bug this fixes. `players` comes from nflverse and only lists people
    on an NFL roster that season, so a taxi-squad stash or someone out of the
    league had no name anywhere and showed up on his own roster as "(unknown
    player)". Sleeper knows them; we were not storing it."""
    from advisor.sources import sleeper

    monkeypatch.setattr(
        sleeper,
        "get_all_players",
        lambda refresh=False: {
            **POOL,
            "9999": {"full_name": "Out Of The League", "position": "WR",
                     "team": None, "status": "Inactive", "active": False},
        },
    )

    assert refresh.sync_players() == 6

    who = refresh.identify(["9999"])["9999"]
    assert who["full_name"] == "Out Of The League"
    assert who["team"] is None, "no NFL team is an answer, not a gap"
    assert who["status"] == "Inactive"


def test_identity_does_not_wait_for_the_mirror(league_db):
    """Importing 12k rows takes seconds and only runs on paths already spending
    them, so the mirror is behind on a fresh install and the week the dump
    updates. A manager must still see his own player's name."""
    query("DELETE FROM sleeper_players")

    who = refresh.identify(["p4"])["p4"]

    assert who["full_name"] == "Free Agent", "read straight from the cached dump"


def test_identity_prefers_the_mirror_to_the_nflverse_crosswalk(league_db):
    """Sleeper is current; nflverse is per-season history. For "who is on this
    roster right now", the current one wins."""
    refresh.sync_players()
    query(
        "INSERT INTO players (player_id, season, sleeper_id, full_name, position, team) "
        "VALUES ('00-9', 2025, 'p1', 'Stale Name', 'RB', 'OLD')"
    )

    assert refresh.identify(["p1"])["p1"]["full_name"] == "Rostered One"


def test_an_unknown_id_is_simply_absent(league_db):
    """No invented row. The caller decides what to show for an id nobody has
    ever heard of."""
    assert refresh.identify(["not-a-player"]) == {}


def test_the_mirror_is_not_reimported_when_it_is_current(league_db):
    refresh.sync_players()

    assert refresh.sync_players() == 0, "the dump has not changed"
    assert refresh.sync_players(force=True) > 0


def test_sync_players_survives_an_unreachable_sleeper(league_db, monkeypatch):
    from advisor.sources import sleeper
    from advisor.sources.sleeper import SleeperError

    def down(refresh=False):
        raise SleeperError("no network")

    monkeypatch.setattr(sleeper, "get_all_players", down)

    assert refresh.sync_players() == 0
    assert refresh.identify(["p1"]) == {}, "no names, but no exception either"


def test_last_synced_reports_when_it_last_worked(league_db):
    assert refresh.last_synced("L1") is None

    refresh.refresh_league("L1")

    assert refresh.last_synced("L1")


def test_a_warehouse_without_the_sync_table_heals_itself(league_db):
    """Every warehouse built before this feature lacks it, and this runs on
    every page load — a missing table would break the app at launch."""
    refresh.refresh_league("L1")
    query("DROP TABLE IF EXISTS league_sync")
    refresh._recent.clear()

    assert refresh.refresh_league("L1").ok
