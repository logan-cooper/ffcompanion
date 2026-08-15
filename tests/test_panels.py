"""The browsing panels: your roster, the other teams, the wire, the table.

The property worth defending here is **agreement**. A sidebar showing 14.2 for
a player the answer beside it calls 12.8 is worse than no sidebar — this app's
whole claim is that its numbers are traceable. So these assert that the panels
and the tools produce the same numbers, not merely that each produces some.

Hermetic where the logic allows it; the agreement tests need real rosters and
real stats, so they run against the linked warehouse and skip without one.
"""

from __future__ import annotations

import json

import pytest

from advisor.context import load_context
from advisor.db import query
from advisor.web import panels


@pytest.fixture
def any_league(linked_leagues):
    """A real linked league with rosters, and the roster that owns it.

    Read-only. Anything that needs to *arrange* a situation uses `fake_league`
    below — the linked warehouse is the user's own data, and a test that mutates
    it leaves the app wrong if it is interrupted between the edit and the undo.
    """
    from advisor.context import list_leagues

    rows = [row for row in list_leagues() if row["roster_id"] is not None]
    if not rows:
        pytest.skip("no linked league with an identifiable roster")
    return load_context(rows[0]["league_id"], roster_id=rows[0]["roster_id"])


LINEUP = ["QB", "RB", "RB", "WR", "FLEX", "BN", "BN", "BN"]


@pytest.fixture
def fake_league(temp_db):
    """A league with rosters and no stats at all.

    Enough to exercise the shapes — lineup slots, empty slots, the table — with
    nothing to restore afterwards. With an empty `player_week_stats` every
    player comes back unscored, which is itself a state the panel has to handle.
    """
    from advisor.warehouse.schema import create_schema

    create_schema()
    query(
        """
        INSERT INTO leagues (league_id, season, name, status, total_rosters,
                             sleeper_type, format, format_source, superflex,
                             has_taxi, is_continuation, roster_positions,
                             scoring_settings, settings, fetched_at)
        VALUES ('L1', 2025, 'Fake Dynasty', 'complete', 3, 2, 'dynasty',
                'settings.type', false, false, false, ?, '{}', '{}', now())
        """,
        [json.dumps(LINEUP)],
    )
    for roster_id, (user, wins, losses, points) in enumerate(
        [("alice", 9, 5, 1600.0), ("bob", 11, 3, 1900.0), ("carol", 4, 10, 1200.0)],
        start=1,
    ):
        query(
            "INSERT INTO league_users (league_id, user_id, display_name, team_name) "
            "VALUES ('L1', ?, ?, NULL)",
            [f"U{roster_id}", user],
        )
        query(
            """
            INSERT INTO league_rosters (league_id, roster_id, owner_id, players,
                                        starters, taxi, reserve, wins, losses,
                                        ties, fpts)
            VALUES ('L1', ?, ?, ?, ?, '[]', '[]', ?, ?, 0, ?)
            """,
            [
                roster_id,
                f"U{roster_id}",
                json.dumps([f"p{roster_id}{n}" for n in range(1, 8)]),
                json.dumps([f"p{roster_id}{n}" for n in range(1, 6)]),
                wins,
                losses,
                points,
            ],
        )
    return load_context("L1", roster_id=1)


# ------------------------------------------------------- the agreement rule

def test_the_panel_and_the_tool_report_the_same_player(any_league):
    """One number per player, whatever asked for it. Both go through
    `player_index` and `player_entry` so that this stays true by construction —
    this test is what notices if someone re-derives one of them."""
    from advisor.tools.rosters import get_my_roster

    panel = panels.roster_panel(any_league)
    tool = get_my_roster(any_league)

    by_id = {}
    for group in ("starters", "bench", "taxi", "reserve"):
        for entry in tool.get(group, []):
            by_id[entry["player_id"]] = entry

    compared = 0
    for group in ("starters", "bench", "taxi", "reserve"):
        for entry in panel.get(group, []):
            other = by_id.get(entry.get("player_id"))
            if other is None:
                continue
            for field in ("season_points", "points_per_game",
                          "last3_points_per_game", "games", "win_now", "future"):
                assert entry.get(field) == other.get(field), (
                    f"{entry['name']}.{field}: panel {entry.get(field)} "
                    f"vs tool {other.get(field)}"
                )
            compared += 1

    assert compared > 3, "nothing was actually compared"


def test_the_waiver_panel_and_the_waiver_tool_agree(any_league):
    from advisor.tools.waivers import get_available_players

    panel = panels.waivers_panel(any_league)
    tool = get_available_players(any_league)
    if "error" in panel or "error" in tool:
        pytest.skip("no free agents with production in this league")

    # The tool takes fewer, so its list must be this one's prefix. A different
    # ordering would mean the wire's top add depends on who is asking.
    top = panel["available"][: len(tool["available"])]
    assert [p["player_id"] for p in top] == [
        p["player_id"] for p in tool["available"]
    ]


def test_the_panel_shows_more_than_the_model_does(any_league):
    """The reason the panels exist at all: a browser is not a context window,
    so the truncation the tools do for a token budget is wrong here."""
    from advisor.tools.waivers import MAX_LIMIT, get_available_players

    panel = panels.waivers_panel(any_league)
    tool = get_available_players(any_league)
    if "error" in panel or "error" in tool:
        pytest.skip("no free agents with production in this league")

    assert len(tool["available"]) <= MAX_LIMIT
    assert panel["total"] > MAX_LIMIT, "this league has too few free agents to tell"
    assert len(panel["available"]) > len(tool["available"])


# --------------------------------------------------------------- the lineup

def test_starters_are_labelled_with_the_slot_they_fill(any_league):
    """Sleeper's `starters` array is positional against the non-bench slots of
    `roster_positions`. That is what turns a flat list into a lineup, and it is
    asserted against a real league because that alignment is a claim about
    Sleeper's data, not about this code."""
    panel = panels.roster_panel(any_league)
    lineup = [p["slot"] for p in panel["starters"]]

    expected = [
        slot for slot in any_league.roster_positions
        if slot not in panels.NON_STARTING_SLOTS
    ]
    assert lineup == expected
    assert len(panel["starters"]) == len(expected)


def test_a_lineup_that_does_not_line_up_is_left_unlabelled(fake_league):
    """Guessing is worse than not saying. A mislabelled slot is a wrong answer
    wearing a UI, not a cosmetic flaw."""
    from dataclasses import replace

    shortened = replace(fake_league, roster_positions=["QB", "BN"])

    assert panels._lineup_slots(shortened, ["a", "b", "c"]) is None
    assert all(
        p["slot"] is None for p in panels.roster_panel(shortened)["starters"]
    )


def test_an_empty_starting_slot_stays_in_the_lineup(fake_league):
    """Sleeper writes "0" into a slot nobody fills. Dropping it would shift
    every player below it up into the wrong slot."""
    query(
        "UPDATE league_rosters SET starters = ? "
        "WHERE league_id = 'L1' AND roster_id = 1",
        [json.dumps(["p11", panels.EMPTY_SLOT, "p13", "p14", "p15"])],
    )

    panel = panels.roster_panel(fake_league)

    assert panel["starters"][1]["empty"] is True
    assert [p["slot"] for p in panel["starters"]] == ["QB", "RB", "RB", "WR", "FLEX"]
    assert "empty" not in panel["starters"][2], "the slot below did not shift up"


def test_a_rostered_player_with_no_stats_is_named_not_dropped(any_league):
    """Rookies and practice-squad call-ups. The tools count them and move on,
    which is right for a token budget; a roster panel that silently showed 27 of
    31 players would just look broken."""
    panel = panels.roster_panel(any_league)
    unscored = [
        p
        for group in ("starters", "bench", "taxi", "reserve")
        for p in panel[group]
        if p.get("no_stats")
    ]
    if not unscored:
        pytest.skip("every rostered player has stats in this league")

    for player in unscored:
        assert player["name"], "an unscored player still has a name"
        # Labelled, because a blank 0.0 beside a real name reads as "he was
        # terrible" rather than "he did not play".
        assert player["no_stats"].strip()


def test_nobody_on_any_roster_is_anonymous(linked_leagues):
    """The reported bug, checked across every team in every linked league.

    `players` is nflverse and lists only people on an NFL roster that season, so
    anyone stashed on a taxi squad or out of the league had no name — on his own
    manager's roster. Sleeper knows all of them.
    """
    from advisor.context import list_leagues

    anonymous = []
    for row in list_leagues():
        ctx = load_context(row["league_id"], roster_id=row["roster_id"])
        for team in query(
            "SELECT roster_id FROM league_rosters WHERE league_id = ?",
            [ctx.league_id],
        ):
            panel = panels.roster_panel(ctx, team["roster_id"])
            for group in ("starters", "bench", "taxi", "reserve"):
                for player in panel[group]:
                    if player.get("empty"):
                        continue
                    if "Sleeper player" in (player.get("name") or ""):
                        anonymous.append((row["name"], team["roster_id"], player))

    assert not anonymous, f"unidentified players: {anonymous[:5]}"


def test_the_reason_for_no_stats_is_specific(any_league):
    """"No NFL team" is a different fact from "did not play this season", and
    the manager can act on the difference."""
    panel = panels.roster_panel(any_league)
    reasons = {
        p["no_stats"]
        for group in ("starters", "bench", "taxi", "reserve")
        for p in panel[group]
        if p.get("no_stats")
    }
    if not reasons:
        pytest.skip("every rostered player has stats in this league")

    assert all("unknown" not in reason for reason in reasons)


def test_the_panel_and_the_tool_name_the_same_unscored_players(any_league):
    """The agreement rule, applied to the players with no numbers. A sidebar
    reading "Tyrone Broden" beside an answer saying "2 players with no stats" is
    exactly the disagreement this app cannot afford."""
    from advisor.tools.rosters import get_my_roster

    panel = panels.roster_panel(any_league)
    tool = get_my_roster(any_league)

    in_panel = {
        p["name"]
        for group in ("starters", "bench", "taxi", "reserve")
        for p in panel[group]
        if p.get("no_stats")
    }
    in_tool = {p["name"] for p in tool.get("rostered_without_stats", [])}

    assert in_panel == in_tool


def test_every_rostered_player_appears_somewhere(any_league):
    """The count is the point of the panel. Nothing may be quietly dropped."""
    row = query(
        "SELECT players, taxi, reserve FROM league_rosters "
        "WHERE league_id = ? AND roster_id = ?",
        [any_league.league_id, any_league.roster_id],
    )[0]
    held = {
        str(p)
        for key in ("players", "taxi", "reserve")
        for p in json.loads(row[key] or "[]")
    }

    panel = panels.roster_panel(any_league)
    shown = sum(
        len([p for p in panel[group] if not p.get("empty")])
        for group in ("starters", "bench", "taxi", "reserve")
    )
    assert shown == len(held)


# ------------------------------------------------------------- the standings

def test_standings_rank_by_record_then_points(fake_league):
    panel = panels.standings_panel(fake_league)

    assert [row["team"] for row in panel["standings"]] == ["bob", "alice", "carol"]
    assert [row["rank"] for row in panel["standings"]] == [1, 2, 3]
    assert panel["standings"][0]["record"] == "11-3"


def test_standings_on_a_real_league_are_in_table_order(any_league):
    panel = panels.standings_panel(any_league)
    if "error" in panel:
        pytest.skip("league has no rosters")

    keys = [(-row["wins"], row["losses"], -row["points_for"])
            for row in panel["standings"]]
    assert keys == sorted(keys), "the table must be in table order"
    assert [row["rank"] for row in panel["standings"]] == list(
        range(1, len(panel["standings"]) + 1)
    )


def test_the_standings_mark_which_team_is_yours(any_league):
    panel = panels.standings_panel(any_league)
    if "error" in panel:
        pytest.skip("league has no rosters")

    mine = [row for row in panel["standings"] if row["is_you"]]
    assert len(mine) == 1
    assert mine[0]["roster_id"] == any_league.roster_id


def test_a_season_with_no_games_is_not_reported_as_a_table(fake_league):
    """Everyone at 0-0 is not a three-way tie for first — it is February."""
    query("UPDATE league_rosters SET wins = 0, losses = 0, ties = 0")

    panel = panels.standings_panel(fake_league)

    assert "no 2025 games played yet" in panel["note"]
    assert "not a table" in panel["note"]


# ------------------------------------------------------------------ labels

def test_a_finished_season_says_why_win_now_is_zero(any_league):
    """With no games left win_now is 0 for everyone by arithmetic. Unlabelled,
    a column of zeroes beside real players reads as a verdict on them."""
    panel = panels.roster_panel(any_league)

    if any_league.games_remaining > 0:
        assert "win_now_is_zero_for_everyone" not in panel
    else:
        assert "not a judgement" in panel["win_now_is_zero_for_everyone"]


def test_panels_state_which_season_the_numbers_are_from(any_league):
    """The app must work all year. In the offseason every figure is last
    season's, and a panel that did not say so would be quietly wrong."""
    for panel in (
        panels.roster_panel(any_league),
        panels.standings_panel(any_league),
        panels.waivers_panel(any_league),
    ):
        assert panel["stats_season"]
        assert panel["league_id"] == any_league.league_id


def test_an_unknown_position_is_refused_with_the_list(fake_league):
    panel = panels.waivers_panel(fake_league, position="FLEX")

    assert "error" in panel
    assert "QB" in panel["positions"], "say what is valid, not just what is not"


def test_a_league_with_no_stats_still_renders(fake_league):
    """A league linked before `make warehouse` has run. Every player is
    unscored, which must come back as a roster with names on it rather than an
    empty panel or a traceback."""
    panel = panels.roster_panel(fake_league)

    assert len(panel["starters"]) == 5
    assert len(panel["bench"]) == 2
    assert all(p.get("no_stats") for p in panel["starters"])
    assert panel["team"] == "alice"
    assert panel["record"] == "9-5"
