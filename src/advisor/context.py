"""LeagueContext — everything a request needs to know about *this* league.

Resolved once per request and passed down. **This is the only place league
format is read.** No valuation strategy, tool body, or schema field should
branch on redraft vs dynasty; if one needs to, the valuation interface is wrong
and should be fixed there instead.

The roadmap places this in `tools/context.py` at Phase 4. It lives here because
`valuation/` (Phase 3b) needs the type and sits *below* the tool layer —
importing upward would invert the dependency. Phase 4's loader can import it
from here unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from advisor.db import query
from advisor.league_format import (
    DEFAULT_TEAM_INTENT,
    MULTI_YEAR_FORMATS,
    UNKNOWN,
)
from advisor.players import latest_season_with_data

# 2025 is the last completed season in the warehouse.
DEFAULT_SEASON = 2025
REGULAR_SEASON_WEEKS = 18

# Games one team plays in a regular season (18 weeks, one bye).
GAMES_PER_SEASON = 17


@dataclass(frozen=True)
class LeagueContext:
    """Immutable snapshot of a league plus the requesting team's stance."""

    league_id: str
    season: int
    name: str = ""
    format: str = UNKNOWN
    superflex: bool = False
    team_intent: str = DEFAULT_TEAM_INTENT
    roster_id: int | None = None
    total_rosters: int = 0
    # Weeks of `season` already played. 0 means the season has not started —
    # which is the normal state for a dynasty league from February to September.
    current_week: int = 0
    # Most recent season the warehouse actually has stats for. Differs from
    # `season` during the offseason, when we value 2026 off 2025 data.
    stats_season: int | None = None
    scoring_settings: dict[str, Any] = field(default_factory=dict)
    roster_positions: list[str] = field(default_factory=list)

    @property
    def is_multi_year(self) -> bool:
        """True when a player's value extends past this season."""
        return self.format in MULTI_YEAR_FORMATS

    @property
    def needs_format_confirmation(self) -> bool:
        return self.format == UNKNOWN

    @property
    def season_started(self) -> bool:
        return self.current_week > 0

    @property
    def season_complete(self) -> bool:
        return self.current_week >= REGULAR_SEASON_WEEKS

    @property
    def is_offseason(self) -> bool:
        """No games played yet in the season being valued.

        Dynasty trades happen year-round, and this is when most of them happen.
        """
        return not self.season_started

    @property
    def games_remaining(self) -> int:
        """Games left in `season`. A full slate before kickoff, not zero."""
        if self.is_offseason:
            return GAMES_PER_SEASON
        return max(0, REGULAR_SEASON_WEEKS - self.current_week)


def weeks_completed(season: int) -> int:
    """Regular-season weeks with stats for `season`. 0 before kickoff."""
    rows = query(
        "SELECT MAX(week) AS week FROM player_week_stats "
        "WHERE season = ? AND season_type = 'REG'",
        [season],
    )
    return rows[0]["week"] or 0 if rows else 0


def load_context(
    league_id: str,
    *,
    roster_id: int | None = None,
    season: int | None = None,
    current_week: int | None = None,
) -> LeagueContext:
    """Build the context for one league, reading intent if a roster is named."""
    rows = query(
        """
        SELECT league_id, season, name, format, superflex, total_rosters,
               scoring_settings, roster_positions
        FROM leagues WHERE league_id = ?
        """,
        [league_id],
    )
    if not rows:
        raise LookupError(f"league {league_id!r} is not linked; run link-league")
    league = rows[0]

    intent = DEFAULT_TEAM_INTENT
    if roster_id is not None:
        intent_rows = query(
            "SELECT intent FROM team_intent WHERE league_id = ? AND roster_id = ?",
            [league_id, roster_id],
        )
        if intent_rows:
            intent = intent_rows[0]["intent"]

    resolved_season = season or league["season"] or DEFAULT_SEASON

    return LeagueContext(
        league_id=league["league_id"],
        season=resolved_season,
        name=league["name"] or "",
        format=league["format"] or UNKNOWN,
        superflex=bool(league["superflex"]),
        team_intent=intent,
        roster_id=roster_id,
        total_rosters=league["total_rosters"] or 0,
        # Inferred from the data rather than assumed complete, so the same code
        # works in March, in week 2, and in December.
        current_week=(
            current_week
            if current_week is not None
            else weeks_completed(resolved_season)
        ),
        stats_season=latest_season_with_data(resolved_season),
        scoring_settings=json.loads(league["scoring_settings"] or "{}"),
        roster_positions=json.loads(league["roster_positions"] or "[]"),
    )
