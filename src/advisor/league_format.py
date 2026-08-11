"""Detect a league's format and superflex status from its Sleeper settings.

Pure functions over a raw league dict — no database, no network. Phase 4's
LeagueContext is the only thing that should read the result.

**The enum values below were verified against real leagues, not documentation.**
Getting this wrong silently reframes every recommendation the app makes, so the
mapping records which values are confirmed and which are inherited from docs:

    type=2  dynasty   CONFIRMED (Wolfpack Dynasty, Beer Ball Empire)
    type=3  survival  CONFIRMED (814 Survival)
    type=0  redraft   documented only — no live league available to check
    type=1  keeper    documented only — no live league available to check

`survival` is not in the roadmap's enum. It was found in real data and is kept
distinct on purpose: a survival league has no persistent rosters, so answering
it with redraft logic would produce confident nonsense. Better for the app to
recognise the format and decline than to guess.

Anything unrecognised resolves to `unknown`, which the app must treat as "ask
the user" rather than defaulting to a format.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

REDRAFT = "redraft"
KEEPER = "keeper"
DYNASTY = "dynasty"
SURVIVAL = "survival"
UNKNOWN = "unknown"

FORMATS = (REDRAFT, KEEPER, DYNASTY, SURVIVAL, UNKNOWN)

# Formats where a player's value extends past this season.
MULTI_YEAR_FORMATS = frozenset({KEEPER, DYNASTY})

SLEEPER_TYPE_FORMATS: dict[int, str] = {
    0: REDRAFT,
    1: KEEPER,
    2: DYNASTY,
    3: SURVIVAL,
}

CONTEND = "contend"
REBUILD = "rebuild"
BALANCED = "balanced"
TEAM_INTENTS = (CONTEND, REBUILD, BALANCED)
DEFAULT_TEAM_INTENT = BALANCED


@dataclass(frozen=True)
class FormatDetection:
    """What we concluded, and how — so a wrong call is debuggable."""

    format: str
    source: str
    sleeper_type: int | None
    superflex: bool
    has_taxi: bool
    is_continuation: bool

    @property
    def is_multi_year(self) -> bool:
        """True when future value matters, i.e. keeper or dynasty."""
        return self.format in MULTI_YEAR_FORMATS

    @property
    def needs_user_confirmation(self) -> bool:
        return self.format == UNKNOWN


def detect_superflex(roster_positions: list[str] | None) -> bool:
    """True when the league lets a second QB into the lineup.

    Covers both an explicit SUPER_FLEX slot and a 2QB build, which changes QB
    valuation the same way.
    """
    positions = roster_positions or []
    return "SUPER_FLEX" in positions or positions.count("QB") >= 2


def detect_format(league: dict[str, Any]) -> FormatDetection:
    """Classify a raw Sleeper league object."""
    settings = league.get("settings") or {}
    raw_type = settings.get("type")
    sleeper_type = raw_type if isinstance(raw_type, int) else None

    has_taxi = bool(settings.get("taxi_slots") or 0)
    is_continuation = bool(league.get("previous_league_id"))
    superflex = detect_superflex(league.get("roster_positions"))

    def result(fmt: str, source: str) -> FormatDetection:
        return FormatDetection(
            format=fmt,
            source=source,
            sleeper_type=sleeper_type,
            superflex=superflex,
            has_taxi=has_taxi,
            is_continuation=is_continuation,
        )

    if sleeper_type is None:
        return result(UNKNOWN, "settings.type missing")

    fmt = SLEEPER_TYPE_FORMATS.get(sleeper_type)
    if fmt is None:
        # A value Sleeper added since this was written. Refuse to guess.
        return result(UNKNOWN, f"unrecognised settings.type={sleeper_type}")

    # Corroborate the redraft call against structure. Taxi squads and a
    # previous_league_id are both carry-over features; if either is present the
    # league is not really a redraft, whatever `type` claims.
    if fmt == REDRAFT and (has_taxi or is_continuation):
        contradiction = "taxi_slots" if has_taxi else "previous_league_id"
        return result(UNKNOWN, f"settings.type=0 contradicted by {contradiction}")

    return result(fmt, f"settings.type={sleeper_type}")
