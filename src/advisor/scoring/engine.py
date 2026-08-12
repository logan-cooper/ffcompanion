"""Turn a raw stat line into fantasy points under a specific league's rules.

Format-agnostic on purpose: this answers one question — "how many points did
this stat line produce under these rules" — and the answer is identical in
redraft and dynasty. Age and format logic belong in `valuation/`, not here.

Nothing is hard-coded about PPR, TD values, or bonuses. Every number comes from
the league's own `scoring_settings`, stored verbatim in Phase 2.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from advisor.scoring.keys import (
    FIRST_DOWN_KEYS,
    KNOWN_UNSUPPORTED_KEYS,
    POSITION_RECEPTION_BONUSES,
    STAT_KEY_COLUMNS,
    YARDAGE_MILESTONE_BONUSES,
    is_offensive_key,
)


@dataclass
class ScoreBreakdown:
    """Points plus where they came from, so any total can be explained."""

    total: float
    contributions: dict[str, float] = field(default_factory=dict)
    unsupported_keys: set[str] = field(default_factory=set)

    def explain(self) -> str:
        parts = [
            f"{key} {value:+.2f}"
            for key, value in sorted(
                self.contributions.items(), key=lambda kv: -abs(kv[1])
            )
        ]
        return f"{self.total:.2f} = " + ", ".join(parts)


def _number(value: Any) -> float:
    """Coerce a stat value to a number. Missing/NULL counts as zero."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def score_stat_line_detailed(
    raw_stats: Mapping[str, Any],
    scoring_settings: Mapping[str, Any],
    *,
    position: str | None = None,
) -> ScoreBreakdown:
    """Score one stat line, returning the per-key breakdown.

    `position` is only needed for positional reception bonuses (the tight end
    premium). Absent keys score zero rather than raising — Sleeper omits any
    rule a league has left at its default.
    """
    contributions: dict[str, float] = {}
    unsupported: set[str] = set()

    for key, raw_value in scoring_settings.items():
        points_per_unit = _number(raw_value)

        if not is_offensive_key(key):
            continue
        if points_per_unit == 0:
            continue  # a rule set to zero cannot move the total

        column = STAT_KEY_COLUMNS.get(key)
        if column is not None:
            units = _number(raw_stats.get(column))
            if units:
                contributions[key] = units * points_per_unit
            continue

        if key in FIRST_DOWN_KEYS:
            fd_column, td_column = FIRST_DOWN_KEYS[key]
            # A touchdown is a first down to nflverse but not to Sleeper.
            units = max(
                0.0,
                _number(raw_stats.get(fd_column)) - _number(raw_stats.get(td_column)),
            )
            if units:
                contributions[key] = units * points_per_unit
            continue

        if key in POSITION_RECEPTION_BONUSES:
            if position == POSITION_RECEPTION_BONUSES[key]:
                receptions = _number(raw_stats.get("receptions"))
                if receptions:
                    contributions[key] = receptions * points_per_unit
            continue

        milestone = next(
            (m for m in YARDAGE_MILESTONE_BONUSES if m[0] == key), None
        )
        if milestone is not None:
            _, milestone_column, threshold = milestone
            if _number(raw_stats.get(milestone_column)) >= threshold:
                contributions[key] = points_per_unit
            continue

        if key in KNOWN_UNSUPPORTED_KEYS:
            unsupported.add(key)
            continue

        # An offensive key we have never seen. Surface it rather than silently
        # dropping points — a missing rule is a wrong answer, not a rounding
        # error, and this is how new Sleeper keys get noticed.
        unsupported.add(key)

    return ScoreBreakdown(
        total=round(sum(contributions.values()), 2),
        contributions=contributions,
        unsupported_keys=unsupported,
    )


def score_stat_line(
    raw_stats: Mapping[str, Any],
    scoring_settings: Mapping[str, Any],
    *,
    position: str | None = None,
) -> float:
    """Points for one stat line under one league's rules."""
    return score_stat_line_detailed(
        raw_stats, scoring_settings, position=position
    ).total
