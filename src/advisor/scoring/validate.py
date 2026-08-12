"""Check the scoring engine against the points Sleeper actually recorded.

The unit tests pin the arithmetic; this pins the *mapping*. Sleeper's matchup
endpoint publishes its own computed points for every rostered player, every
week, so scoring the warehouse's stat lines and diffing gives an independent
check over thousands of player-weeks rather than a handful of spot checks.

Needs network. Run it after touching `scoring/keys.py` — a wrong entry there
produces plausible numbers that are quietly wrong, which no unit test on
hand-written stat lines will catch.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

from advisor.db import query
from advisor.scoring.engine import score_stat_line_detailed
from advisor.sources import sleeper

MATCH_TOLERANCE = 0.02
REGULAR_SEASON_WEEKS = 18


@dataclass
class ValidationResult:
    league_id: str
    league_name: str
    compared: int = 0
    exact: int = 0
    residuals: Counter = field(default_factory=Counter)
    unsupported_keys: set[str] = field(default_factory=set)

    @property
    def rate(self) -> float:
        return self.exact / self.compared if self.compared else 0.0

    def __str__(self) -> str:
        line = (
            f"{self.league_name:<22}{self.exact:>6}/{self.compared:<6}"
            f"= {self.rate:7.2%}"
        )
        if self.residuals:
            worst = ", ".join(
                f"{diff:+g} x{count}" for diff, count in self.residuals.most_common(3)
            )
            line += f"   residual: {worst}"
        if self.unsupported_keys:
            line += f"\n{'':22}unsupported: {sorted(self.unsupported_keys)}"
        return line


def _stat_lines(season: int) -> dict[tuple[str, int], dict]:
    return {
        (row["sleeper_id"], row["week"]): row
        for row in query(
            """
            SELECT s.*, p.sleeper_id, p.position AS pos
            FROM player_week_stats s
            JOIN players p ON p.player_id = s.player_id AND p.season = s.season
            WHERE s.season = ? AND s.season_type = 'REG' AND p.sleeper_id IS NOT NULL
            """,
            [season],
        )
    }


def validate_league(league: dict, stat_lines: dict, season: int) -> ValidationResult:
    settings = json.loads(league["scoring_settings"])
    result = ValidationResult(league["league_id"], league["name"])

    for week in range(1, REGULAR_SEASON_WEEKS + 1):
        for matchup in sleeper.get_matchups(league["league_id"], week):
            for sleeper_id, recorded in (matchup.get("players_points") or {}).items():
                stats = stat_lines.get((sleeper_id, week))
                if stats is None:
                    continue

                breakdown = score_stat_line_detailed(
                    stats, settings, position=stats["pos"]
                )
                result.unsupported_keys |= breakdown.unsupported_keys
                result.compared += 1

                difference = round(breakdown.total - recorded, 2)
                if abs(difference) < MATCH_TOLERANCE:
                    result.exact += 1
                else:
                    result.residuals[difference] += 1

    return result


def validate_all(season: int) -> list[ValidationResult]:
    stat_lines = _stat_lines(season)
    leagues = query(
        "SELECT league_id, name, scoring_settings FROM leagues ORDER BY name"
    )
    if not leagues:
        raise LookupError("no leagues linked; run: make link-league")
    return [validate_league(lg, stat_lines, season) for lg in leagues]
