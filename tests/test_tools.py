"""Phase 4: the six tools.

The gate is that every tool works with a real league and that the *same* call
with the *same* arguments returns different numbers under dynasty and redraft
while keeping an identical shape. If the shapes diverged, the tool layer would
be branching on format; if the numbers didn't, the valuation layer would be
decorative.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from advisor.context import load_context
from advisor.db import query
from advisor.tools import (
    REGISTRY,
    TOOLS,
    clear_index_cache,
    compare_players,
    evaluate_trade,
    get_available_players,
    get_league_rosters,
    get_my_roster,
    parse_pick,
    resolve_player,
    validate_registry,
)
from advisor.tools.base import MAX_RESPONSE_CHARS, estimated_tokens
from advisor.tools.compare import MAX_PLAYERS, MAX_WEEKS
from advisor.tools.waivers import MAX_LIMIT

SEASON = 2025
DEMO_WEEK = 14


@pytest.fixture
def ctx(linked_leagues):
    clear_index_cache()
    rows = query("SELECT league_id FROM leagues WHERE name = 'Wolfpack Dynasty'")
    if not rows:
        pytest.skip("Wolfpack Dynasty not linked")
    return load_context(rows[0]["league_id"], roster_id=8, current_week=DEMO_WEEK)


@pytest.fixture
def redraft_ctx(ctx):
    """Same league and data, reinterpreted as a single-year format."""
    return dataclasses.replace(ctx, format="redraft")


def player_id(name: str, ctx) -> str:
    result = resolve_player(name, ctx)
    candidates = result.get("candidates") or []
    if not candidates:
        pytest.skip(f"{name} not resolvable")
    return candidates[0]["player_id"]


def all_responses(ctx) -> dict[str, dict]:
    pid = player_id("Puka Nacua", ctx)
    return {
        "resolve_player": resolve_player("nacua", ctx),
        "get_my_roster": get_my_roster(ctx, 8),
        "get_league_rosters": get_league_rosters(ctx),
        "compare_players": compare_players(ctx, [pid], weeks=4),
        "get_available_players": get_available_players(ctx, "RB", limit=5),
        "evaluate_trade": evaluate_trade(ctx, 8, 1, [pid], ["2027-1st"]),
    }


# ------------------------------------------------------------------ registry

def test_registry_and_schemas_agree():
    validate_registry()


def test_no_schema_asks_the_model_for_session_state():
    """`league_id` and the user's own roster are bound by the agent loop.

    They were originally in every schema. A real trace showed the model passing
    the league's *name* where an id belonged — it cannot know these values, so
    asking for them only creates wrong calls. Every argument removed is one
    fewer way a small local model produces something unusable.
    """
    for tool in TOOLS:
        schema = tool["input_schema"]
        assert "league_id" not in schema["properties"], tool["name"]
        assert "league_id" not in schema["required"], tool["name"]
        # roster_id may be offered (to ask about *another* team) but never required.
        assert "roster_id" not in schema["required"], tool["name"]
        assert "my_roster_id" not in schema["required"], tool["name"]


def test_session_context_wins_over_a_rebuilt_one(ctx):
    """The loop passes the live context; tools must not rebuild their own.

    Rebuilding from league_id resets current_week to "now" and team_intent to
    the default — which silently answered a week-14 contending session as a
    week-18 balanced one.
    """
    payload = REGISTRY["get_league_rosters"](ctx=ctx)
    assert payload["as_of"]["current_week"] == ctx.current_week
    assert payload["league"]["team_intent"] == ctx.team_intent


def test_schemas_are_json_serialisable():
    json.dumps(TOOLS)


def test_descriptions_are_substantial():
    """Descriptions are prompt engineering, not documentation — a one-liner here
    is how the model ends up calling the wrong tool."""
    for tool in TOOLS:
        assert len(tool["description"]) > 200, tool["name"]


def test_resolve_player_description_tells_the_model_to_call_it_first():
    description = next(t for t in TOOLS if t["name"] == "resolve_player")["description"]
    assert "FIRST" in description


def test_trade_description_says_it_returns_no_verdict():
    description = next(t for t in TOOLS if t["name"] == "evaluate_trade")["description"]
    assert "NO VERDICT" in description.upper()


def test_registry_callables_accept_league_id(ctx):
    result = REGISTRY["get_league_rosters"](league_id=ctx.league_id)
    assert "rosters" in result


# ------------------------------------------------------------------ envelope

def test_every_response_carries_format_intent_and_season_phase(ctx):
    for name, payload in all_responses(ctx).items():
        assert payload["league"]["format"] == "dynasty", name
        assert payload["league"]["team_intent"], name
        assert "current_week" in payload["as_of"], name
        assert payload["as_of"]["stats_from_season"] == SEASON, name
        assert payload["as_of"]["basis"], name


def test_every_response_carries_data_as_of(ctx):
    for name, payload in all_responses(ctx).items():
        assert payload["as_of"]["data_as_of"], name


def test_every_response_fits_the_token_budget(ctx):
    for name, payload in all_responses(ctx).items():
        size = len(json.dumps(payload, default=str))
        assert size <= MAX_RESPONSE_CHARS, f"{name} is {estimated_tokens(payload)} tokens"


def test_a_completed_season_explains_why_win_now_is_zero(ctx):
    """`win_now: 0.0` beside an elite player reads as a verdict unless labelled."""
    complete = dataclasses.replace(ctx, current_week=18)
    payload = get_my_roster(complete, 8)
    assert "win_now_is_zero_for_everyone" in payload["as_of"]
    assert all(p["win_now"] == 0.0 for p in payload["starters"])


def test_offseason_basis_says_the_season_has_not_started(ctx):
    offseason = dataclasses.replace(
        ctx, season=SEASON + 1, current_week=0, stats_season=SEASON
    )
    payload = get_league_rosters(offseason)
    assert "has not started" in payload["as_of"]["basis"]


# ------------------------------------------------------------- resolve_player

def test_resolve_player_finds_an_exact_name(ctx):
    result = resolve_player("Puka Nacua", ctx)
    assert result["candidates"][0]["name"] == "Puka Nacua"


def test_resolve_player_handles_suffixes_and_punctuation(ctx):
    for spelling in ("Marvin Harrison Jr.", "marvin harrison", "MARVIN HARRISON JR"):
        result = resolve_player(spelling, ctx)
        assert result["candidates"][0]["name"] == "Marvin Harrison Jr.", spelling


def test_a_surname_ranks_by_production_not_name_position(ctx):
    """'harrison' must surface the star receiver, not a kicker whose first name
    happens to match. This is the app's #1 failure mode."""
    names = [c["name"] for c in resolve_player("harrison", ctx)["candidates"]]
    assert names[0] == "Marvin Harrison Jr."


def test_multiple_matches_are_flagged_as_ambiguous(ctx):
    result = resolve_player("allen", ctx)
    assert len(result["candidates"]) > 1
    assert "ambiguous" in result


def test_a_single_match_is_not_flagged_ambiguous(ctx):
    assert "ambiguous" not in resolve_player("Puka Nacua", ctx)


def test_resolve_player_reports_the_current_owner(ctx):
    owner = resolve_player("Puka Nacua", ctx)["candidates"][0]["owner"]
    assert owner == "free agent" or "roster_id" in owner


def test_unknown_name_is_an_error_not_an_empty_list(ctx):
    result = resolve_player("Zxqv Nonexistent", ctx)
    assert "error" in result and result["detail"]
    assert "candidates" not in result


def test_empty_query_is_an_error(ctx):
    assert "error" in resolve_player("   ", ctx)


# -------------------------------------------------------------- get_my_roster

def test_my_roster_splits_slots_and_reports_games(ctx):
    payload = get_my_roster(ctx, 8)
    assert payload["starters"] and payload["bench"]
    for player in payload["starters"]:
        # `games` must travel with every total — it separates "declined" from
        # "was hurt".
        assert "games" in player
        assert "season_points" in player


def test_my_roster_includes_age_and_values_in_dynasty(ctx):
    for player in get_my_roster(ctx, 8)["starters"]:
        assert "age" in player
        assert "win_now" in player and "future" in player


def test_my_roster_future_is_zero_in_a_single_year_format(redraft_ctx):
    for player in get_my_roster(redraft_ctx, 8)["starters"]:
        assert player["future"] == 0.0


def test_my_roster_returns_picks_when_the_format_has_them(ctx):
    payload = get_my_roster(ctx, 8)
    assert "draft_picks" in payload
    assert any(p["future_value"] > 0 for p in payload["draft_picks"])


def test_unknown_roster_is_an_error(ctx):
    assert "error" in get_my_roster(ctx, 999)


# --------------------------------------------------------- get_league_rosters

def test_league_rosters_are_compact_and_cover_every_team(ctx):
    payload = get_league_rosters(ctx)
    assert len(payload["rosters"]) == ctx.total_rosters
    for roster in payload["rosters"]:
        assert "points_per_game_by_position" in roster
        # Compact by design: no per-player stat lines.
        assert "players_detail" not in roster


def test_league_rosters_report_age_only_where_rosters_carry_over(ctx, redraft_ctx):
    dynasty = get_league_rosters(ctx)["rosters"]
    redraft = get_league_rosters(redraft_ctx)["rosters"]
    assert any("avg_age_rb_wr" in r for r in dynasty)
    assert all("avg_age_rb_wr" not in r for r in redraft)


# ------------------------------------------------------------ compare_players

def test_compare_players_caps_the_number_of_players(ctx):
    ids = [p["player_id"] for p in get_my_roster(ctx, 8)["starters"]][:6]
    if len(ids) < 5:
        pytest.skip("not enough rostered players")
    assert len(compare_players(ctx, ids)["players"]) <= MAX_PLAYERS


def test_compare_players_caps_weeks(ctx):
    payload = compare_players(ctx, [player_id("Puka Nacua", ctx)], weeks=99)
    assert payload["weeks_shown"] == MAX_WEEKS
    assert len(payload["players"][0]["weekly_points"]) <= MAX_WEEKS


def test_compare_players_includes_usage(ctx):
    payload = compare_players(ctx, [player_id("Puka Nacua", ctx)])
    usage = payload["players"][0].get("usage", {})
    assert "snap_share" in usage or "target_share" in usage


def test_compare_players_omits_upcoming_when_the_season_is_over(ctx):
    """There is no next opponent in January, and inventing one is exactly the
    fabrication this app exists to avoid."""
    complete = dataclasses.replace(ctx, current_week=18)
    payload = compare_players(complete, [player_id("Puka Nacua", complete)])
    assert "upcoming" not in payload["players"][0]


def test_compare_players_reports_unknown_ids(ctx):
    payload = compare_players(ctx, [player_id("Puka Nacua", ctx), "00-0000000"])
    assert payload["not_found"] == ["00-0000000"]


def test_compare_players_with_only_unknown_ids_is_an_error(ctx):
    assert "error" in compare_players(ctx, ["00-0000000"])


def test_compare_players_with_no_ids_is_an_error(ctx):
    assert "error" in compare_players(ctx, [])


# ------------------------------------------------------- get_available_players

def test_available_players_are_actually_unrostered(ctx):
    rostered = set()
    for roster in get_league_rosters(ctx)["rosters"]:
        for player in get_my_roster(ctx, roster["roster_id"]).get("starters", []):
            rostered.add(player["player_id"])

    for player in get_available_players(ctx, "WR", limit=10)["available"]:
        assert player["player_id"] not in rostered


def test_available_players_respects_the_limit(ctx):
    assert len(get_available_players(ctx, limit=99)["available"]) <= MAX_LIMIT


def test_available_players_are_ranked_by_recent_form(ctx):
    values = [
        p["last3_points_per_game"]
        for p in get_available_players(ctx, "WR", limit=10)["available"]
    ]
    assert values == sorted(values, reverse=True)


def test_available_players_filters_by_position(ctx):
    for player in get_available_players(ctx, "TE", limit=10)["available"]:
        assert player["position"] == "TE"


def test_unknown_position_is_an_error(ctx):
    assert "error" in get_available_players(ctx, "PUNTER")


# ------------------------------------------------------------- evaluate_trade

@pytest.mark.parametrize(
    "reference,expected",
    [
        ("2027-1st", (2027, 1)),
        ("2027-1", (2027, 1)),
        ("2026 2nd", (2026, 2)),
        ("2028-3rd", (2028, 3)),
        ("2027 round 1", (2027, 1)),
        ("00-0039075", None),
        ("Puka Nacua", None),
        ("", None),
    ],
)
def test_pick_reference_parsing(reference, expected):
    assert parse_pick(reference) == expected


def test_trade_accepts_players_and_picks_together(ctx):
    payload = evaluate_trade(
        ctx, 8, 1, [player_id("Christian McCaffrey", ctx)], ["2027-1st"]
    )
    kinds = {a["kind"] for a in payload["my_roster"]["i_get"]["assets"]}
    assert kinds == {"pick"}
    assert payload["my_roster"]["i_give"]["assets"][0]["kind"] == "player"


def test_trade_returns_no_verdict(ctx):
    payload = evaluate_trade(
        ctx, 8, 1, [player_id("Christian McCaffrey", ctx)], ["2027-1st"]
    )
    assert "no_verdict" in payload
    text = json.dumps(payload).lower()
    for word in ("recommend", "should accept", "good trade", "bad trade", "verdict:"):
        assert word not in text


def test_trade_deltas_mirror_between_sides(ctx):
    payload = evaluate_trade(
        ctx, 8, 1, [player_id("Christian McCaffrey", ctx)], ["2027-1st"]
    )
    mine, theirs = payload["my_roster"], payload["their_roster"]
    assert mine["win_now_delta"] == pytest.approx(-theirs["win_now_delta"])
    assert mine["future_delta"] == pytest.approx(-theirs["future_delta"])


def test_picks_are_worthless_in_a_single_year_format(ctx, redraft_ctx):
    give = [player_id("Christian McCaffrey", ctx)]
    dynasty = evaluate_trade(ctx, 8, 1, give, ["2027-1st"])
    redraft = evaluate_trade(redraft_ctx, 8, 1, give, ["2027-1st"])

    assert dynasty["my_roster"]["i_get"]["future_total"] > 0
    assert redraft["my_roster"]["i_get"]["future_total"] == 0.0


def test_the_same_trade_reads_differently_by_format(ctx, redraft_ctx):
    """The Phase 4 gate: identical arguments, identical shape, different numbers."""
    give = [player_id("Christian McCaffrey", ctx)]
    get = [player_id("Tetairoa McMillan", ctx), "2027-1st"]

    dynasty = evaluate_trade(ctx, 8, 1, give, get)["my_roster"]
    redraft = evaluate_trade(redraft_ctx, 8, 1, give, get)["my_roster"]

    assert dynasty.keys() == redraft.keys()  # shape is identical
    assert dynasty["win_now_delta"] == pytest.approx(redraft["win_now_delta"])
    assert dynasty["future_delta"] > redraft["future_delta"] == 0.0
    assert dynasty["intent_weighted_delta"] != redraft["intent_weighted_delta"]


def test_trade_reports_startable_depth_change(ctx):
    payload = evaluate_trade(
        ctx, 8, 1, [player_id("Christian McCaffrey", ctx)], ["2027-1st"]
    )
    depth = payload["my_roster"]["startable_by_position"]
    assert "before" in depth and "after" in depth


def test_unresolvable_assets_are_reported(ctx):
    payload = evaluate_trade(
        ctx, 8, 1, [player_id("Puka Nacua", ctx)], ["not-a-player"]
    )
    assert payload["unresolved"] == ["not-a-player"]


def test_trade_with_nothing_resolvable_is_an_error(ctx):
    assert "error" in evaluate_trade(ctx, 8, 1, ["nope"], ["also-nope"])


def test_empty_trade_is_an_error(ctx):
    assert "error" in evaluate_trade(ctx, 8, 1, [], [])
