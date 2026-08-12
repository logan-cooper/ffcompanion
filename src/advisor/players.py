"""Player identity that survives across seasons.

`players` is keyed `(player_id, season)`, which is right for storage — team and
experience are season-dependent — but wrong as a lookup during the offseason. In
February 2026 a dynasty league is season 2026 and *no* 2026 rows exist yet, so a
naive lookup returns nothing and every player silently values at zero.

This module resolves a player from the most recent season at or before the one
being valued, and reports age **as of the season being valued** rather than the
season the row came from. Valuing 2026 off 2025 rows must not use 2025 ages —
that is a free year of youth on every roster, which is exactly backwards for
dynasty.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache

from advisor.db import query

# Age is measured at opening weekend, so a player's age is stable all season.
SEASON_START_MONTH = 9
SEASON_START_DAY = 1

DAYS_PER_YEAR = 365.25


def age_on_season(birth_date: date | None, season: int) -> float | None:
    """Age at the start of `season`. None when birth date is unknown."""
    if birth_date is None:
        return None
    reference = date(season, SEASON_START_MONTH, SEASON_START_DAY)
    return round((reference - birth_date).days / DAYS_PER_YEAR, 2)


@lru_cache(maxsize=1)
def _available_seasons() -> tuple[int, ...]:
    rows = query("SELECT DISTINCT season FROM players ORDER BY season DESC")
    return tuple(r["season"] for r in rows)


def latest_season_with_data(at_or_before: int | None = None) -> int | None:
    """Most recent season the warehouse has player rows for."""
    seasons = _available_seasons()
    if not seasons:
        return None
    if at_or_before is None:
        return seasons[0]
    eligible = [s for s in seasons if s <= at_or_before]
    return eligible[0] if eligible else None


def clear_caches() -> None:
    """Drop memoised season lists. Call after ingesting a new season."""
    _available_seasons.cache_clear()


def player_profile(player_id: str, season: int) -> dict:
    """Identity for `player_id` as of `season`, falling back to earlier rows.

    Returns `age` for the requested season (not the source row's season) and
    `source_season` so a caller can tell how stale the identity is.
    """
    rows = query(
        """
        SELECT player_id, season, full_name, position, team, birth_date,
               sleeper_id, years_exp, rookie_year, draft_round, draft_pick
        FROM players
        WHERE player_id = ? AND season <= ?
        ORDER BY season DESC LIMIT 1
        """,
        [player_id, season],
    )
    if not rows:
        return {
            "player_id": player_id,
            "full_name": player_id,
            "position": None,
            "team": None,
            "age": None,
            "source_season": None,
            "seasons_stale": None,
        }

    row = rows[0]
    return {
        **row,
        # Recomputed for the valuation season, so an offseason lookup ages
        # everyone forward instead of freezing last season's ages.
        "age": age_on_season(row["birth_date"], season),
        "source_season": row["season"],
        "seasons_stale": season - row["season"],
    }
