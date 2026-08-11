"""Populate the warehouse from cached nflverse Parquet.

Idempotent by construction: every load deletes the rows it is about to write
before writing them, scoped to the season (and, where the data is weekly, to the
specific weeks). Re-running `make ingest SEASON=2025` replaces rows rather than
duplicating them, and re-running it for a single week leaves the rest alone.

All SQL goes through `db.query()` — this module never imports duckdb.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from advisor.db import query
from advisor.sources import nflverse
from advisor.warehouse.schema import create_schema

# Weekly tables are logged per week; these are logged once per season.
SEASON_LEVEL_SOURCES = ("players", "schedules")


@dataclass
class IngestSummary:
    """Row counts written per table, for CLI output and tests."""

    season: int
    weeks: tuple[int, ...] | None
    rows: dict[str, int] = field(default_factory=dict)

    def __str__(self) -> str:
        scope = "all weeks" if self.weeks is None else f"weeks {list(self.weeks)}"
        lines = [f"Ingested season {self.season} ({scope}):"]
        lines += [f"  {table:<20} {count:>7,} rows" for table, count in self.rows.items()]
        return "\n".join(lines)


def _week_filter(weeks: Sequence[int] | None, column: str = "week") -> tuple[str, list]:
    """Return an optional `AND week IN (...)` fragment plus its parameters."""
    if not weeks:
        return "", []
    placeholders = ", ".join("?" for _ in weeks)
    return f" AND {column} IN ({placeholders})", list(weeks)


def _count(table: str, season: int, weeks: Sequence[int] | None) -> int:
    clause, params = _week_filter(weeks)
    if table in SEASON_LEVEL_SOURCES:
        clause, params = "", []
    rows = query(
        f"SELECT COUNT(*) AS n FROM {table} WHERE season = ?{clause}",
        [season, *params],
    )
    return int(rows[0]["n"])


def _log(source: str, season: int, weeks: Sequence[int] | None) -> None:
    """Record what was just written so staleness is answerable later."""
    fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
    season_level = source in SEASON_LEVEL_SOURCES
    # Season-level rows carry week IS NULL, which no `week IN (...)` can match.
    clause, params = ("", []) if season_level else _week_filter(weeks)

    query(
        f"DELETE FROM ingest_log WHERE source = ? AND season = ?{clause}",
        [source, season, *params],
    )

    if season_level:
        query(
            f"INSERT INTO ingest_log (source, season, week, row_count, fetched_at) "
            f"SELECT ?, ?, NULL, COUNT(*), ? FROM {source} WHERE season = ?",
            [source, season, fetched_at, season],
        )
        return

    query(
        f"INSERT INTO ingest_log (source, season, week, row_count, fetched_at) "
        f"SELECT ?, ?, week, COUNT(*), ? FROM {source} "
        f"WHERE season = ?{clause} GROUP BY week",
        [source, season, fetched_at, season, *params],
    )


def _load_players(season: int, players_pq: Path, rosters_pq: Path, stats_pq: Path) -> None:
    """Build the player dimension by joining the roster crosswalk to the
    league-wide player table.

    Neither source alone is enough: `rosters` carries `sleeper_id` (the crosswalk
    the tool layer needs) but no draft position, while `players` carries
    birth date and draft position but no Sleeper id.
    """
    query("DELETE FROM players WHERE season = ?", [season])
    query(
        """
        INSERT INTO players
        WITH roster AS (
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY gsis_id ORDER BY week DESC NULLS LAST, team
                ) AS rn
                FROM read_parquet(?)
                WHERE gsis_id IS NOT NULL
            ) WHERE rn = 1
        ),
        ids AS (
            SELECT gsis_id AS player_id FROM roster
            UNION
            SELECT DISTINCT player_id FROM read_parquet(?) WHERE player_id IS NOT NULL
        )
        SELECT
            ids.player_id,
            ? AS season,
            r.sleeper_id,
            COALESCE(p.pfr_id, r.pfr_id)                       AS pfr_id,
            COALESCE(p.display_name, r.full_name)              AS full_name,
            COALESCE(r.position, p.position)                   AS position,
            r.team,
            COALESCE(TRY_CAST(p.birth_date AS DATE), r.birth_date) AS birth_date,
            DATE_DIFF(
                'day',
                COALESCE(TRY_CAST(p.birth_date AS DATE), r.birth_date),
                CAST(? AS DATE)
            ) / 365.25                                         AS age,
            COALESCE(r.years_exp, p.years_of_experience)       AS years_exp,
            COALESCE(r.rookie_year, p.rookie_season)           AS rookie_year,
            p.draft_round,
            p.draft_pick
        FROM ids
        LEFT JOIN roster r        ON r.gsis_id = ids.player_id
        LEFT JOIN read_parquet(?) p ON p.gsis_id = ids.player_id
        """,
        [
            str(rosters_pq),
            str(stats_pq),
            season,
            f"{season}-09-01",  # age is measured as of opening weekend
            str(players_pq),
        ],
    )


def _load_player_week_stats(
    season: int, stats_pq: Path, weeks: Sequence[int] | None
) -> None:
    clause, params = _week_filter(weeks)
    query(
        f"DELETE FROM player_week_stats WHERE season = ?{clause}", [season, *params]
    )
    query(
        f"""
        INSERT INTO player_week_stats
        SELECT
            player_id, season, week, season_type, position, team, opponent_team,
            completions, attempts, passing_yards, passing_tds,
            passing_interceptions AS interceptions,
            carries, rushing_yards, rushing_tds,
            receptions, targets, receiving_yards, receiving_tds,
            COALESCE(sack_fumbles_lost, 0)
                + COALESCE(rushing_fumbles_lost, 0)
                + COALESCE(receiving_fumbles_lost, 0) AS fumbles_lost,
            passing_2pt_conversions,
            rushing_2pt_conversions,
            receiving_2pt_conversions,
            special_teams_tds
        FROM read_parquet(?)
        WHERE season = ? AND player_id IS NOT NULL{clause}
        """,
        [str(stats_pq), season, *params],
    )


def _load_player_week_usage(
    season: int,
    stats_pq: Path,
    snaps_pq: Path,
    red_zone_pq: Path,
    players_pq: Path,
    weeks: Sequence[int] | None,
) -> None:
    """Assemble usage from three differently-keyed sources.

    Target and air-yards share ship with the weekly stats; snap share comes from
    PFR data keyed by `pfr_player_id` and has to be crosswalked back to gsis ids;
    red-zone volume isn't published at all and is counted from play-by-play.
    """
    clause, params = _week_filter(weeks)
    query(
        f"DELETE FROM player_week_usage WHERE season = ?{clause}", [season, *params]
    )
    query(
        f"""
        INSERT INTO player_week_usage
        WITH base AS (
            SELECT player_id, season, week, season_type, target_share, air_yards_share
            FROM read_parquet(?)
            WHERE season = ? AND player_id IS NOT NULL{clause}
        ),
        crosswalk AS (
            SELECT pfr_id, gsis_id FROM read_parquet(?)
            WHERE pfr_id IS NOT NULL AND gsis_id IS NOT NULL
        ),
        snaps AS (
            SELECT
                c.gsis_id            AS player_id,
                s.season,
                s.week,
                -- snap counts label playoff rounds WC/DIV/CON/SB where the
                -- weekly stats just say POST; collapse so the join lands.
                CASE WHEN s.game_type = 'REG' THEN 'REG' ELSE 'POST' END
                                     AS season_type,
                MAX(s.offense_pct)   AS snap_share
            FROM read_parquet(?) s
            JOIN crosswalk c ON c.pfr_id = s.pfr_player_id
            WHERE s.season = ?
            GROUP BY 1, 2, 3, 4
        ),
        red_zone_long AS (
            SELECT season, week, season_type,
                   rusher_player_id AS player_id,
                   1 AS carries, 0 AS targets
            FROM read_parquet(?)
            WHERE rusher_player_id IS NOT NULL AND rush_attempt = 1
            UNION ALL
            SELECT season, week, season_type,
                   receiver_player_id AS player_id,
                   0 AS carries, 1 AS targets
            FROM read_parquet(?)
            WHERE receiver_player_id IS NOT NULL AND pass_attempt = 1
        ),
        red_zone AS (
            SELECT player_id, season, week, season_type,
                   SUM(carries) AS red_zone_carries,
                   SUM(targets) AS red_zone_targets
            FROM red_zone_long
            GROUP BY 1, 2, 3, 4
        )
        SELECT
            b.player_id,
            b.season,
            b.week,
            b.season_type,
            sn.snap_share,
            b.target_share,
            b.air_yards_share,
            COALESCE(rz.red_zone_carries, 0) AS red_zone_carries,
            COALESCE(rz.red_zone_targets, 0) AS red_zone_targets,
            COALESCE(rz.red_zone_carries, 0)
                + COALESCE(rz.red_zone_targets, 0) AS red_zone_touches
        FROM base b
        LEFT JOIN snaps sn
            ON  sn.player_id = b.player_id AND sn.season = b.season
            AND sn.week = b.week AND sn.season_type = b.season_type
        LEFT JOIN red_zone rz
            ON  rz.player_id = b.player_id AND rz.season = b.season
            AND rz.week = b.week AND rz.season_type = b.season_type
        """,
        [
            str(stats_pq),
            season,
            *params,
            str(players_pq),
            str(snaps_pq),
            season,
            str(red_zone_pq),
            str(red_zone_pq),
        ],
    )


def _load_schedules(season: int, schedules_pq: Path) -> None:
    query("DELETE FROM schedules WHERE season = ?", [season])
    query(
        """
        INSERT INTO schedules
        SELECT game_id, season, week, game_type,
               TRY_CAST(gameday AS DATE) AS gameday,
               home_team, away_team, home_score, away_score
        FROM read_parquet(?)
        WHERE season = ? AND game_id IS NOT NULL
        """,
        [str(schedules_pq), season],
    )


def ingest_season(
    season: int,
    *,
    weeks: Sequence[int] | None = None,
    refresh: bool = False,
) -> IngestSummary:
    """Fetch and load one season. Returns row counts per table.

    `weeks` limits the weekly tables to specific weeks; the player dimension and
    schedule are always rewritten in full since they're season-scoped anyway.
    `refresh` re-downloads instead of reusing the Parquet cache.
    """
    create_schema()

    stats_pq = nflverse.fetch_player_stats(season, refresh=refresh)
    players_pq = nflverse.fetch_players(refresh=refresh)
    rosters_pq = nflverse.fetch_rosters(season, refresh=refresh)
    schedules_pq = nflverse.fetch_schedules(season, refresh=refresh)
    snaps_pq = nflverse.fetch_snap_counts(season, refresh=refresh)
    red_zone_pq = nflverse.fetch_red_zone_plays(season, refresh=refresh)

    _load_players(season, players_pq, rosters_pq, stats_pq)
    _load_player_week_stats(season, stats_pq, weeks)
    _load_player_week_usage(
        season, stats_pq, snaps_pq, red_zone_pq, players_pq, weeks
    )
    _load_schedules(season, schedules_pq)

    for source in ("players", "player_week_stats", "player_week_usage", "schedules"):
        _log(source, season, weeks)

    normalized_weeks = tuple(weeks) if weeks else None
    return IngestSummary(
        season=season,
        weeks=normalized_weeks,
        rows={
            table: _count(table, season, weeks)
            for table in (
                "players",
                "player_week_stats",
                "player_week_usage",
                "schedules",
            )
        },
    )
