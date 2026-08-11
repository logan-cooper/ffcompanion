"""Fetch nflverse datasets and cache them as Parquet under `data/cache/`.

This module's only job is getting bytes onto disk. It does no schema work and
touches no database — the warehouse layer reads the cached Parquet with DuckDB.

`nflreadpy` resolves the nflverse release URLs (which get renamed periodically,
so it is worth not hand-coding them) but caches only in memory by default. The
Parquet cache written here is what makes re-runs fast and offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import nflreadpy as nfl
import polars as pl

from advisor.config import get_settings

# Columns pulled from each raw dataset. Everything else is dropped before the
# cache is written — notably `fantasy_points`/`fantasy_points_ppr`, which the
# warehouse deliberately does not store (scoring is per-league, see Phase 3).
PLAYER_STATS_COLUMNS = [
    "player_id",
    "player_display_name",
    "position",
    "season",
    "week",
    "season_type",
    "team",
    "opponent_team",
    "completions",
    "attempts",
    "passing_yards",
    "passing_tds",
    "passing_interceptions",
    "sack_fumbles_lost",
    "carries",
    "rushing_yards",
    "rushing_tds",
    "rushing_fumbles_lost",
    "receptions",
    "targets",
    "receiving_yards",
    "receiving_tds",
    "receiving_fumbles_lost",
    "passing_2pt_conversions",
    "rushing_2pt_conversions",
    "receiving_2pt_conversions",
    "special_teams_tds",
    "target_share",
    "air_yards_share",
]

PLAYERS_COLUMNS = [
    "gsis_id",
    "pfr_id",
    "display_name",
    "position",
    "birth_date",
    "rookie_season",
    "years_of_experience",
    "draft_year",
    "draft_round",
    "draft_pick",
]

ROSTERS_COLUMNS = [
    "season",
    "week",  # used only to pick a traded player's most recent team
    "team",
    "position",
    "full_name",
    "gsis_id",
    "sleeper_id",
    "pfr_id",
    "birth_date",
    "years_exp",
    "rookie_year",
]

SCHEDULES_COLUMNS = [
    "game_id",
    "season",
    "game_type",
    "week",
    "gameday",
    "home_team",
    "away_team",
    "home_score",
    "away_score",
]

SNAP_COUNTS_COLUMNS = [
    "season",
    "game_type",
    "week",
    "pfr_player_id",
    "team",
    "offense_snaps",
    "offense_pct",
]

# Slim play-by-play, filtered to red-zone snaps. The full 2025 table is ~49k
# rows x 372 columns; we keep six columns of the plays inside the 20.
PBP_RED_ZONE_COLUMNS = [
    "season",
    "week",
    "season_type",
    "rush_attempt",
    "pass_attempt",
    "rusher_player_id",
    "receiver_player_id",
]

RED_ZONE_YARDLINE = 20


def cache_dir() -> Path:
    """Directory holding cached raw Parquet. Created on demand."""
    path = get_settings().cache_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cached(
    name: str,
    build: Callable[[], pl.DataFrame],
    *,
    refresh: bool = False,
) -> Path:
    """Return the path to `name`.parquet, building it via `build()` if needed."""
    path = cache_dir() / f"{name}.parquet"
    if path.exists() and not refresh:
        return path

    frame = build()
    # Write to a temp file first so an interrupted download can't leave a
    # truncated Parquet that later runs would happily treat as cached.
    tmp = path.with_suffix(".parquet.tmp")
    frame.write_parquet(tmp)
    tmp.replace(path)
    return path


def _select(frame: pl.DataFrame, columns: list[str]) -> pl.DataFrame:
    """Select `columns`, tolerating any the upstream dataset has dropped."""
    available = [c for c in columns if c in frame.columns]
    missing = set(columns) - set(available)
    if missing:
        raise KeyError(
            f"nflverse dataset is missing expected columns: {sorted(missing)}. "
            "The upstream schema likely changed; update the column list."
        )
    return frame.select(available)


def fetch_player_stats(season: int, *, refresh: bool = False) -> Path:
    """Weekly player counting stats."""
    return _cached(
        f"player_stats_{season}",
        lambda: _select(nfl.load_player_stats(seasons=[season]), PLAYER_STATS_COLUMNS),
        refresh=refresh,
    )


def fetch_players(*, refresh: bool = False) -> Path:
    """League-wide player dimension: birth date, draft position, ids."""
    return _cached(
        "players",
        lambda: _select(nfl.load_players(), PLAYERS_COLUMNS),
        refresh=refresh,
    )


def fetch_rosters(season: int, *, refresh: bool = False) -> Path:
    """Season rosters — the source of the nflverse-to-Sleeper id crosswalk."""
    return _cached(
        f"rosters_{season}",
        lambda: _select(nfl.load_rosters(seasons=[season]), ROSTERS_COLUMNS),
        refresh=refresh,
    )


def fetch_schedules(season: int, *, refresh: bool = False) -> Path:
    """Game schedule, so "who does he play next" is answerable."""
    return _cached(
        f"schedules_{season}",
        lambda: _select(nfl.load_schedules(seasons=[season]), SCHEDULES_COLUMNS),
        refresh=refresh,
    )


def fetch_snap_counts(season: int, *, refresh: bool = False) -> Path:
    """Weekly snap counts, keyed by PFR id rather than gsis id."""
    return _cached(
        f"snap_counts_{season}",
        lambda: _select(nfl.load_snap_counts(seasons=[season]), SNAP_COUNTS_COLUMNS),
        refresh=refresh,
    )


def fetch_red_zone_plays(season: int, *, refresh: bool = False) -> Path:
    """Play-by-play rows inside the opponent 20, slimmed to the ids we count.

    Red-zone usage isn't published as a weekly stat, so it has to be derived
    from play-by-play. Filtering and column-pruning happens before the cache is
    written to keep this file small.
    """

    def build() -> pl.DataFrame:
        pbp = nfl.load_pbp(seasons=[season])
        red_zone = pbp.filter(
            pl.col("yardline_100").is_not_null()
            & (pl.col("yardline_100") <= RED_ZONE_YARDLINE)
        )
        return _select(red_zone, PBP_RED_ZONE_COLUMNS)

    return _cached(f"pbp_red_zone_{season}", build, refresh=refresh)
