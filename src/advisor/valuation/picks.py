"""Draft pick values, as static consensus data.

Deriving your own pick-value chart from scratch is a research project, not a
feature — the roadmap defers it explicitly. These are consensus-style relative
values with an early/mid/late 1st split, scaled into the same
points-above-replacement units players are measured in so the two can be added.

Sleeper's `traded_picks` gives only season and round, never slot, so `MID` is
the default. A caller who knows a team is likely picking early can say so.
"""

from __future__ import annotations

from enum import Enum


class PickSlot(str, Enum):
    EARLY = "early"
    MID = "mid"
    LATE = "late"


# Relative chart values, 1.01 = 100. Shape follows published dynasty consensus:
# the top of the first is worth multiples of its bottom, and value falls off a
# cliff after the second round.
PICK_CHART: dict[int, dict[PickSlot, float]] = {
    1: {PickSlot.EARLY: 100.0, PickSlot.MID: 68.0, PickSlot.LATE: 45.0},
    2: {PickSlot.EARLY: 32.0, PickSlot.MID: 24.0, PickSlot.LATE: 17.0},
    3: {PickSlot.EARLY: 12.0, PickSlot.MID: 9.0, PickSlot.LATE: 6.0},
    4: {PickSlot.EARLY: 4.0, PickSlot.MID: 3.0, PickSlot.LATE: 2.0},
}

# Chart points -> season points above replacement. Calibration knob: an early
# 1st (100 chart points) is treated as worth roughly a 45-PAR asset, i.e. a
# good-but-not-elite starter's season. Tune here, not in the chart.
CHART_POINTS_TO_PAR = 0.45

# A pick two years out is worth less than the same pick this year: more time for
# the roster to change, and rookies are lottery tickets either way. Applied per
# season of distance, on top of the valuation's own discount rate.
DISTANCE_DISCOUNT = 0.85

# Picks beyond this are noise.
MAX_VALUED_ROUND = 4


def pick_chart_points(round_: int, slot: PickSlot = PickSlot.MID) -> float:
    """Raw consensus chart value for a pick."""
    return PICK_CHART.get(round_, {}).get(slot, 0.0)


def pick_par_value(
    round_: int,
    *,
    seasons_away: int = 1,
    slot: PickSlot = PickSlot.MID,
    superflex: bool = False,
) -> float:
    """Pick value in points-above-replacement units.

    `superflex` lifts early picks: in a superflex league a first-round rookie
    pick is more likely to return a startable quarterback, which is the scarcest
    thing in the format.
    """
    if round_ > MAX_VALUED_ROUND:
        return 0.0

    value = pick_chart_points(round_, slot) * CHART_POINTS_TO_PAR
    value *= DISTANCE_DISCOUNT ** max(0, seasons_away)

    if superflex and round_ == 1:
        value *= 1.15

    return round(value, 2)
