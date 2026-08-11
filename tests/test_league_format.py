"""Format detection. The roadmap flags this as the thing everything downstream
depends on, so it is tested against every enum value we know of plus the cases
where `settings.type` cannot be trusted."""

from __future__ import annotations

import pytest

from advisor.league_format import (
    DYNASTY,
    KEEPER,
    REDRAFT,
    SURVIVAL,
    UNKNOWN,
    detect_format,
    detect_superflex,
)
from tests.fixtures import leagues as fx


@pytest.mark.parametrize(
    "league,expected",
    [
        (fx.REDRAFT, REDRAFT),
        (fx.KEEPER, KEEPER),
        (fx.DYNASTY_SUPERFLEX, DYNASTY),
        (fx.SURVIVAL, SURVIVAL),
    ],
)
def test_known_types_map_to_formats(league, expected):
    assert detect_format(league).format == expected


def test_the_two_real_leagues_are_classified_differently():
    """The gate: a dynasty league and a non-dynasty league must not agree."""
    dynasty = detect_format(fx.DYNASTY_SUPERFLEX)
    survival = detect_format(fx.SURVIVAL)

    assert dynasty.format != survival.format
    assert dynasty.is_multi_year
    assert not survival.is_multi_year


def test_redraft_and_dynasty_differ():
    """Same assertion for the pair the roadmap actually names.

    Uses the written redraft fixture because no live redraft league exists on
    this Sleeper account.
    """
    assert detect_format(fx.REDRAFT).format != detect_format(fx.DYNASTY_SUPERFLEX).format
    assert not detect_format(fx.REDRAFT).is_multi_year
    assert detect_format(fx.DYNASTY_SUPERFLEX).is_multi_year


@pytest.mark.parametrize(
    "league",
    [
        fx.REDRAFT_CONTRADICTED_BY_TAXI,
        fx.REDRAFT_CONTRADICTED_BY_CONTINUATION,
        fx.UNRECOGNISED_TYPE,
        fx.MISSING_TYPE,
    ],
)
def test_ambiguous_leagues_resolve_to_unknown(league):
    """Never default silently — an unresolved format must make the app ask."""
    detection = detect_format(league)
    assert detection.format == UNKNOWN
    assert detection.needs_user_confirmation
    assert detection.source  # always explains itself


def test_unknown_is_not_treated_as_multi_year():
    assert not detect_format(fx.MISSING_TYPE).is_multi_year


def test_detection_records_the_raw_sleeper_type():
    """Raw type is kept so a future enum change is diagnosable from the data."""
    assert detect_format(fx.DYNASTY_SUPERFLEX).sleeper_type == 2
    assert detect_format(fx.SURVIVAL).sleeper_type == 3
    assert detect_format(fx.UNRECOGNISED_TYPE).sleeper_type == 99


def test_structural_flags_are_captured():
    dynasty = detect_format(fx.DYNASTY_SUPERFLEX)
    assert dynasty.has_taxi
    assert dynasty.is_continuation

    survival = detect_format(fx.SURVIVAL)
    assert not survival.has_taxi
    assert not survival.is_continuation


@pytest.mark.parametrize(
    "positions,expected",
    [
        (fx.SUPERFLEX_POSITIONS, True),
        (fx.TWO_QB_POSITIONS, True),  # 2QB moves QB value the same way
        (fx.STANDARD_POSITIONS, False),
        ([], False),
        (None, False),
    ],
)
def test_superflex_detection(positions, expected):
    assert detect_superflex(positions) is expected


def test_superflex_is_detected_on_real_dynasty_league():
    assert detect_format(fx.DYNASTY_SUPERFLEX).superflex is True
    assert detect_format(fx.SURVIVAL).superflex is False
