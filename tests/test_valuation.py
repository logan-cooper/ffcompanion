"""Phase 3b gate: the format abstraction is real, not decorative.

The headline test trades an aging productive back for a younger, lesser
receiver and asserts the verdict flips three ways — a loss in redraft, a gain
under dynasty+rebuild, and back to a loss under dynasty+contend. If that passes,
format and intent genuinely change the answer.
"""

from __future__ import annotations

import dataclasses

import pytest

from advisor.context import LeagueContext, load_context
from advisor.db import query
from advisor.league_format import BALANCED, CONTEND, DYNASTY, REBUILD, SURVIVAL, UNKNOWN
from advisor.valuation import (
    DynastyValuation,
    RedraftValuation,
    clear_caches,
    combined_value,
    get_valuation,
)
from advisor.valuation.aging import aging_multiplier, relative_multiplier
from advisor.valuation.intent import INTENT_WEIGHTS, weigh
from advisor.valuation.picks import PickSlot, pick_par_value

SEASON = 2025

# An aging productive RB for a younger, lower-producing WR.
GIVE = "Christian McCaffrey"  # 29.2 — win_now 41.0, future 60.4
GET = "Tetairoa McMillan"  # 22.4 — win_now 14.4, future 122.8

# Pinned by name rather than "first dynasty league": replacement level differs
# between the two linked leagues (Beer Ball rosters 12 teams to Wolfpack's 10),
# and a gate test that silently changes league is a gate test that proves
# nothing.
GATE_LEAGUE = "Wolfpack Dynasty"


@pytest.fixture
def dynasty_ctx(linked_leagues):
    clear_caches()
    rows = query(
        "SELECT league_id FROM leagues WHERE format = 'dynasty' AND name = ?",
        [GATE_LEAGUE],
    )
    if not rows:
        pytest.skip(f"{GATE_LEAGUE} not linked")
    return load_context(rows[0]["league_id"], roster_id=8, current_week=14)


def _player_id(name: str) -> str:
    rows = query(
        "SELECT player_id FROM players WHERE season = ? AND full_name = ?",
        [SEASON, name],
    )
    if not rows:
        pytest.skip(f"{name} not in warehouse")
    return rows[0]["player_id"]


# ------------------------------------------------------------------ the gate

def test_the_same_trade_flips_with_format_and_intent(dynasty_ctx):
    """The Phase 3b gate.

    Give an aging producer, get a younger lesser player:
      redraft            -> loss (you gave up this season's points)
      dynasty + rebuild  -> gain (you bought years)
      dynasty + contend  -> loss again (you are trying to win now)
    """
    give_id, get_id = _player_id(GIVE), _player_id(GET)
    redraft, dynasty = RedraftValuation(), DynastyValuation()

    # Redraft: only this season counts.
    redraft_delta = (
        redraft.player_value(get_id, dynasty_ctx).win_now
        - redraft.player_value(give_id, dynasty_ctx).win_now
    )
    assert redraft_delta < 0, "aging producer should win a redraft trade"

    give = dynasty.player_value(give_id, dynasty_ctx)
    get = dynasty.player_value(get_id, dynasty_ctx)

    rebuild_ctx = dataclasses.replace(dynasty_ctx, team_intent=REBUILD)
    contend_ctx = dataclasses.replace(dynasty_ctx, team_intent=CONTEND)

    rebuild_delta = combined_value(get, rebuild_ctx) - combined_value(give, rebuild_ctx)
    contend_delta = combined_value(get, contend_ctx) - combined_value(give, contend_ctx)

    assert rebuild_delta > 0, "rebuilding team should want the younger player"
    assert contend_delta < 0, "contending team should want the producer"


def test_redraft_ignores_age_entirely(dynasty_ctx):
    """Two players producing identically are worth the same in redraft,
    whatever their ages."""
    redraft = RedraftValuation()
    give = redraft.player_value(_player_id(GIVE), dynasty_ctx)
    get = redraft.player_value(_player_id(GET), dynasty_ctx)

    assert give.future == 0.0
    assert get.future == 0.0
    # The older, more productive player is simply worth more.
    assert give.win_now > get.win_now


def test_dynasty_gives_the_younger_player_more_future(dynasty_ctx):
    dynasty = DynastyValuation()
    give = dynasty.player_value(_player_id(GIVE), dynasty_ctx)
    get = dynasty.player_value(_player_id(GET), dynasty_ctx)

    assert give.win_now > get.win_now  # still the better player today
    assert get.future > give.future  # but not the better asset
    # The aging back keeps real future value — he does not fall off a cliff the
    # moment the season ends. Zeroing him out would be as wrong as ignoring age.
    assert give.future > 0


def test_win_now_is_identical_across_strategies(dynasty_ctx):
    """Dynasty composes redraft for win-now rather than reimplementing it, so
    the two must never disagree."""
    player_id = _player_id(GIVE)
    assert (
        DynastyValuation().player_value(player_id, dynasty_ctx).win_now
        == RedraftValuation().player_value(player_id, dynasty_ctx).win_now
    )


# ---------------------------------------------------------------- the factory

@pytest.mark.parametrize(
    "format_,expected",
    [
        (DYNASTY, "dynasty"),
        ("keeper", "dynasty"),  # keepers carry over; redraft would be more wrong
        ("redraft", "redraft"),
        (SURVIVAL, "redraft"),  # no persistent rosters
        (UNKNOWN, "redraft"),  # conservative: no speculative future value
    ],
)
def test_factory_picks_the_strategy(format_, expected):
    ctx = LeagueContext(league_id="L", season=SEASON, format=format_)
    assert get_valuation(ctx).name == expected


def test_picks_are_worthless_in_redraft():
    ctx = LeagueContext(league_id="L", season=SEASON, format="redraft")
    pick = RedraftValuation().pick_value(SEASON + 1, 1, ctx)
    assert pick.total == 0.0


def test_picks_are_assets_in_dynasty():
    ctx = LeagueContext(league_id="L", season=SEASON, format=DYNASTY)
    first = DynastyValuation().pick_value(SEASON + 1, 1, ctx)
    third = DynastyValuation().pick_value(SEASON + 1, 3, ctx)

    assert first.future > third.future > 0
    assert first.win_now == 0.0  # a future pick helps you later, not now
    assert first.label == f"{SEASON + 1}-1st"


def test_pick_value_decays_with_distance():
    near = pick_par_value(1, seasons_away=1)
    far = pick_par_value(1, seasons_away=3)
    assert near > far > 0


def test_pick_slots_are_ordered():
    early = pick_par_value(1, slot=PickSlot.EARLY)
    mid = pick_par_value(1, slot=PickSlot.MID)
    late = pick_par_value(1, slot=PickSlot.LATE)
    assert early > mid > late


def test_superflex_lifts_first_round_picks():
    assert pick_par_value(1, superflex=True) > pick_par_value(1, superflex=False)


# ------------------------------------------------------------------- aging

def test_running_backs_decline_faster_than_quarterbacks():
    """The single most important shape in dynasty valuation."""
    rb_fall = aging_multiplier("RB", 25) - aging_multiplier("RB", 30)
    qb_fall = aging_multiplier("QB", 25) - aging_multiplier("QB", 30)
    assert rb_fall > qb_fall
    assert aging_multiplier("QB", 30) == pytest.approx(1.0)


def test_receivers_hold_value_longer_than_backs():
    assert aging_multiplier("WR", 29) > aging_multiplier("RB", 29)


def test_tight_ends_peak_late():
    assert aging_multiplier("TE", 22) < aging_multiplier("TE", 27)


def test_aging_curve_is_monotonic_after_peak():
    for position in ("RB", "WR", "TE", "QB"):
        values = [aging_multiplier(position, age) for age in range(30, 40)]
        assert values == sorted(values, reverse=True), position


def test_aging_curve_interpolates_between_anchors():
    low, high = aging_multiplier("RB", 29), aging_multiplier("RB", 30)
    middle = aging_multiplier("RB", 29.5)
    assert high < middle < low


def test_unknown_position_gets_no_age_penalty():
    assert aging_multiplier("K", 30) == 1.0
    assert aging_multiplier(None, 30) == 1.0


def test_missing_age_does_not_crash():
    assert 0 < aging_multiplier("RB", None) <= 1.0
    assert 0 < relative_multiplier("RB", None, 3) <= 1.0


def test_aging_is_applied_relative_to_now_not_to_peak():
    """The curves give share-of-peak, but a projection starts from what a player
    does today — and today's number already reflects today's age. Using the
    absolute value would double-count the decline."""
    # A 29-year-old back declines ~24% next year, not down to 0.44 of output.
    one_year = relative_multiplier("RB", 29, 1)
    assert 0.6 < one_year < 0.9
    assert one_year > aging_multiplier("RB", 30)

    # A player at peak neither gains nor loses much in one year.
    assert relative_multiplier("QB", 28, 1) == pytest.approx(1.0)


def test_relative_multiplier_compounds_downward():
    assert (
        relative_multiplier("RB", 29, 3)
        < relative_multiplier("RB", 29, 2)
        < relative_multiplier("RB", 29, 1)
    )


def test_young_players_are_not_projected_to_multiply():
    for position in ("RB", "WR", "TE", "QB"):
        assert relative_multiplier(position, 21, 3) <= 1.15


# ------------------------------------------------------------------- intent

def test_intent_weights_are_mirror_images():
    """Neither stance should be quietly favoured by the arithmetic."""
    contend, rebuild = INTENT_WEIGHTS[CONTEND], INTENT_WEIGHTS[REBUILD]
    assert contend == tuple(reversed(rebuild))
    for weights in INTENT_WEIGHTS.values():
        assert sum(weights) == pytest.approx(1.0)


def test_intent_cannot_change_a_redraft_answer():
    """Intent is a multi-year concept. A redraft player must be worth the same
    to every team — applying the split anyway would rescale win_now and make
    the same player look four times better to a contender."""
    values = {}
    for intent in (CONTEND, REBUILD, BALANCED):
        ctx = LeagueContext(
            league_id="L", season=SEASON, format="redraft", team_intent=intent
        )
        values[intent] = weigh(win_now=100.0, future=0.0, ctx=ctx).combined

    assert set(values.values()) == {100.0}


def test_survival_and_unknown_also_ignore_intent():
    for format_ in (SURVIVAL, UNKNOWN):
        ctx = LeagueContext(
            league_id="L", season=SEASON, format=format_, team_intent=REBUILD
        )
        assert weigh(80.0, 0.0, ctx).combined == pytest.approx(80.0)


def test_intent_shifts_a_dynasty_answer():
    def combined(intent: str) -> float:
        ctx = LeagueContext(
            league_id="L", season=SEASON, format=DYNASTY, team_intent=intent
        )
        return weigh(win_now=100.0, future=200.0, ctx=ctx).combined

    assert combined(CONTEND) < combined(BALANCED) < combined(REBUILD)


def test_unknown_intent_falls_back_to_balanced():
    ctx = LeagueContext(league_id="L", season=SEASON, team_intent="vibes")
    assert weigh(10.0, 10.0, ctx).combined == pytest.approx(10.0)


def test_weighted_value_explains_itself():
    ctx = LeagueContext(league_id="L", season=SEASON, team_intent=CONTEND)
    text = weigh(50.0, 10.0, ctx).explain()
    assert "win_now" in text and "future" in text and CONTEND in text


# ------------------------------------------------------------------ flooring

def test_below_replacement_players_are_worth_zero_not_negative(dynasty_ctx):
    """Replacement level is free from the wire, so a worse player is worth
    nothing — not a liability you would pay to dump."""
    rows = query(
        """
        SELECT p.player_id FROM players p
        JOIN v_player_season_totals t ON t.player_id=p.player_id AND t.season=p.season
        WHERE p.season=? AND p.position='RB' AND p.age>=30 AND t.games>=8
        ORDER BY t.rushing_yards ASC LIMIT 5
        """,
        [SEASON],
    )
    if not rows:
        pytest.skip("no aging low-production backs in warehouse")

    dynasty = DynastyValuation()
    for row in rows:
        value = dynasty.player_value(row["player_id"], dynasty_ctx)
        assert value.win_now >= 0.0
        assert value.future >= 0.0


# ------------------------------------------------------------------ context

def test_context_reads_format_and_intent_from_the_database(dynasty_ctx):
    assert dynasty_ctx.format == DYNASTY
    assert dynasty_ctx.is_multi_year
    assert not dynasty_ctx.needs_format_confirmation


def test_unknown_format_context_demands_confirmation():
    ctx = LeagueContext(league_id="L", season=SEASON, format=UNKNOWN)
    assert ctx.needs_format_confirmation
    assert not ctx.is_multi_year


def test_loading_an_unlinked_league_raises(warehouse):
    with pytest.raises(LookupError):
        load_context("not-a-league")


def test_roster_value_aggregates_players(dynasty_ctx):
    roster = DynastyValuation().roster_value(dynasty_ctx.roster_id, dynasty_ctx)
    assert roster.players
    assert roster.win_now == pytest.approx(
        sum(p.win_now for p in roster.players) + sum(k.win_now for k in roster.picks),
        abs=0.05,
    )
    assert roster.average_age is not None
