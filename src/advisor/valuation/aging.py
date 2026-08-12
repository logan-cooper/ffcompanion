"""Positional aging curves.

The curve shape is **data you can tune**, not logic you have to rewrite. Each
entry maps age -> the share of a player's current per-game production expected
in a season at that age. Values between anchors are linearly interpolated;
outside the range they clamp to the nearest anchor.

The shapes encode well-established positional patterns:

- **RB** — the sharpest cliff in fantasy. Holds through 25, bends at 26-27, and
  falls off hard after 28. This is what makes a 29-year-old back a sell in
  dynasty even while he is still producing.
- **WR** — a long plateau. Holds through the late twenties and declines
  gradually after 30.
- **TE** — slow to start and late to peak. Rookie tight ends rarely produce, and
  good ones hold value deep into their thirties.
- **QB** — by far the longest shelf life. Barely declines before the late
  thirties, which combined with superflex is why quarterbacks dominate dynasty
  values.

These are deliberately simple. The point is a transparent, arguable curve, not a
fitted one — see the roadmap's note about not building an ML projection.
"""

from __future__ import annotations

from bisect import bisect_left

AgingCurve = tuple[tuple[float, float], ...]

AGING_CURVES: dict[str, AgingCurve] = {
    "RB": (
        (21, 0.95), (22, 1.00), (24, 1.00), (25, 0.98), (26, 0.93),
        (27, 0.84), (28, 0.72), (29, 0.58), (30, 0.44), (31, 0.30),
        (32, 0.20), (34, 0.08), (36, 0.02),
    ),
    "WR": (
        (21, 0.88), (22, 0.94), (23, 0.99), (24, 1.00), (27, 1.00),
        (28, 0.97), (29, 0.92), (30, 0.84), (31, 0.72), (32, 0.58),
        (33, 0.44), (35, 0.22), (37, 0.06),
    ),
    "TE": (
        (21, 0.72), (22, 0.80), (23, 0.88), (24, 0.94), (25, 0.98),
        (26, 1.00), (29, 1.00), (30, 0.96), (31, 0.90), (32, 0.82),
        (33, 0.72), (35, 0.48), (37, 0.20), (39, 0.05),
    ),
    "QB": (
        (21, 0.88), (22, 0.93), (23, 0.97), (24, 1.00), (32, 1.00),
        (33, 0.98), (34, 0.95), (35, 0.90), (36, 0.84), (37, 0.74),
        (38, 0.62), (39, 0.48), (40, 0.34), (42, 0.12), (44, 0.02),
    ),
}

# Used when a player's position has no curve (K, DEF) or is unknown. Flat, so an
# unmapped position never gets a silent age penalty or bonus.
DEFAULT_MULTIPLIER = 1.0

# Applied when age is missing. Slightly below 1.0 because an unknown age is more
# often a fringe player than a star, but not so low that it looks like a verdict.
UNKNOWN_AGE_MULTIPLIER = 0.85


def aging_multiplier(position: str | None, age: float | None) -> float:
    """Share of current production expected at `age` for `position`."""
    if age is None:
        return UNKNOWN_AGE_MULTIPLIER

    curve = AGING_CURVES.get((position or "").upper())
    if not curve:
        return DEFAULT_MULTIPLIER

    ages = [point[0] for point in curve]

    if age <= ages[0]:
        return curve[0][1]
    if age >= ages[-1]:
        return curve[-1][1]

    index = bisect_left(ages, age)
    if ages[index] == age:
        return curve[index][1]

    (age_lo, value_lo), (age_hi, value_hi) = curve[index - 1], curve[index]
    fraction = (age - age_lo) / (age_hi - age_lo)
    return value_lo + fraction * (value_hi - value_lo)


# A young player's curve still rises; cap the upside so a 21-year-old is not
# projected to multiply his production.
MAX_IMPROVEMENT = 1.15


def relative_multiplier(
    position: str | None, age: float | None, years_ahead: int
) -> float:
    """Production `years_ahead` from now, **relative to now**.

    The curves above give share-of-peak, but a projection starts from what a
    player is doing *today* — and today's number already reflects today's age.
    Applying the absolute curve value would double-count the decline: a
    29-year-old back at 0.58 would be projected at 0.44 of his current output
    next season, a 24% drop mis-stated as 56%. The year-over-year change is the
    ratio between the two points on the curve.
    """
    if age is None:
        return UNKNOWN_AGE_MULTIPLIER ** max(1, years_ahead)

    now = aging_multiplier(position, age)
    later = aging_multiplier(position, age + years_ahead)
    if now <= 0:
        return 0.0
    return min(later / now, MAX_IMPROVEMENT)
