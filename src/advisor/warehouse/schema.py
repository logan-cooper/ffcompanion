"""Warehouse DDL: five tables and three derived views.

Two rules this schema exists to enforce:

1. **No fantasy points anywhere.** `player_week_stats` holds raw counting stats
   only. Points depend on a league's scoring settings and are computed at query
   time by `scoring/` (Phase 3). nflverse publishes `fantasy_points` columns and
   they are dropped on purpose.
2. **Dynasty fields are captured now.** Age, draft position, and experience are
   useless to redraft but impossible to backfill without re-ingesting, so
   `players` carries them from the start.
"""

from __future__ import annotations

from advisor.db import query

# Fantasy uses the NFL regular season only; POST is ingested but views ignore it.
REGULAR_SEASON = "REG"

TABLES: dict[str, str] = {
    # Keyed by (player_id, season) rather than player_id alone: team, age, and
    # experience are all season-dependent, and this keeps re-ingesting one
    # season from clobbering another's rows regardless of ingest order.
    "players": """
        CREATE TABLE IF NOT EXISTS players (
            player_id    VARCHAR NOT NULL,  -- nflverse gsis_id
            season       INTEGER NOT NULL,
            sleeper_id   VARCHAR,           -- crosswalk; NULL for ~a third of rows
            pfr_id       VARCHAR,           -- needed to join snap counts
            full_name    VARCHAR,
            position     VARCHAR,
            team         VARCHAR,
            birth_date   DATE,
            age          DOUBLE,            -- years as of Sept 1 of `season`
            years_exp    INTEGER,
            rookie_year  INTEGER,
            draft_round  INTEGER,
            draft_pick   INTEGER,
            PRIMARY KEY (player_id, season)
        )
    """,
    # Raw counting stats only. The three 2pt columns stay separate because
    # Sleeper scores pass_2pt, rush_2pt, and rec_2pt under distinct keys.
    "player_week_stats": """
        CREATE TABLE IF NOT EXISTS player_week_stats (
            player_id                VARCHAR NOT NULL,
            season                   INTEGER NOT NULL,
            week                     INTEGER NOT NULL,
            season_type              VARCHAR NOT NULL,
            position                 VARCHAR,
            team                     VARCHAR,
            opponent_team            VARCHAR,
            completions              INTEGER,
            attempts                 INTEGER,
            passing_yards            DOUBLE,
            passing_tds              INTEGER,
            interceptions            INTEGER,
            carries                  INTEGER,
            rushing_yards            DOUBLE,
            rushing_tds              INTEGER,
            receptions               INTEGER,
            targets                  INTEGER,
            receiving_yards          DOUBLE,
            receiving_tds            INTEGER,
            fumbles                  INTEGER,  -- all fumbles (Sleeper `fum`)
            fumbles_lost             INTEGER,  -- lost only (Sleeper `fum_lost`)
            passing_2pt_conversions  INTEGER,
            rushing_2pt_conversions  INTEGER,
            receiving_2pt_conversions INTEGER,
            special_teams_tds        INTEGER,  -- kick/punt return TDs
            -- Needed by real league scoring: first-down and 40+ yard bonuses
            -- are common enough that omitting them makes points quietly wrong.
            passing_first_downs      INTEGER,
            rushing_first_downs      INTEGER,
            receiving_first_downs    INTEGER,
            passing_40               INTEGER,  -- completions of 40+ yards
            rushing_40               INTEGER,  -- rushes of 40+ yards
            receiving_40             INTEGER,  -- receptions of 40+ yards
            PRIMARY KEY (player_id, season, week, season_type)
        )
    """,
    # red_zone_touches = carries + targets inside the 20. Carries and targets
    # are also stored separately so callers never have to guess the definition.
    "player_week_usage": """
        CREATE TABLE IF NOT EXISTS player_week_usage (
            player_id        VARCHAR NOT NULL,
            season           INTEGER NOT NULL,
            week             INTEGER NOT NULL,
            season_type      VARCHAR NOT NULL,
            snap_share       DOUBLE,   -- 0-1, offensive snaps
            target_share     DOUBLE,   -- 0-1
            air_yards_share  DOUBLE,   -- 0-1
            red_zone_carries INTEGER,
            red_zone_targets INTEGER,
            red_zone_touches INTEGER,
            PRIMARY KEY (player_id, season, week, season_type)
        )
    """,
    "schedules": """
        CREATE TABLE IF NOT EXISTS schedules (
            game_id    VARCHAR NOT NULL,
            season     INTEGER NOT NULL,
            week       INTEGER NOT NULL,
            game_type  VARCHAR,
            gameday    DATE,
            home_team  VARCHAR,
            away_team  VARCHAR,
            home_score INTEGER,
            away_score INTEGER,
            PRIMARY KEY (game_id)
        )
    """,
    # One row per (source, season, week). week is NULL for season-level
    # datasets. This is what answers "is my data stale?".
    "ingest_log": """
        CREATE TABLE IF NOT EXISTS ingest_log (
            source     VARCHAR NOT NULL,
            season     INTEGER NOT NULL,
            week       INTEGER,
            row_count  BIGINT NOT NULL,
            fetched_at TIMESTAMP NOT NULL
        )
    """,
    # scoring_settings and roster_positions are stored verbatim as JSON. The
    # scoring engine (Phase 3) reads a league's own rules; normalising them into
    # columns would mean guessing which keys matter before we know.
    "leagues": """
        CREATE TABLE IF NOT EXISTS leagues (
            league_id          VARCHAR NOT NULL,
            season             INTEGER NOT NULL,
            name               VARCHAR,
            status             VARCHAR,
            total_rosters      INTEGER,
            previous_league_id VARCHAR,
            sleeper_type       INTEGER,   -- raw settings.type, kept for audit
            format             VARCHAR,   -- redraft|keeper|dynasty|survival|unknown
            format_source      VARCHAR,   -- how detection decided
            superflex          BOOLEAN,
            has_taxi           BOOLEAN,
            is_continuation    BOOLEAN,
            roster_positions   JSON,
            scoring_settings   JSON,
            settings           JSON,
            fetched_at         TIMESTAMP NOT NULL,
            PRIMARY KEY (league_id)
        )
    """,
    "league_users": """
        CREATE TABLE IF NOT EXISTS league_users (
            league_id    VARCHAR NOT NULL,
            user_id      VARCHAR NOT NULL,
            display_name VARCHAR,
            team_name    VARCHAR,
            PRIMARY KEY (league_id, user_id)
        )
    """,
    "league_rosters": """
        CREATE TABLE IF NOT EXISTS league_rosters (
            league_id  VARCHAR NOT NULL,
            roster_id  INTEGER NOT NULL,
            owner_id   VARCHAR,
            players    JSON,   -- Sleeper player_ids
            starters   JSON,
            taxi       JSON,
            reserve    JSON,
            wins       INTEGER,
            losses     INTEGER,
            ties       INTEGER,
            fpts       DOUBLE,
            PRIMARY KEY (league_id, roster_id)
        )
    """,
    # Future picks are tradeable dynasty assets. roster_id is the pick's
    # original owner; owner_roster_id is who holds it now.
    "traded_picks": """
        CREATE TABLE IF NOT EXISTS traded_picks (
            league_id                VARCHAR NOT NULL,
            season                   INTEGER NOT NULL,
            round                    INTEGER NOT NULL,
            original_roster_id       INTEGER NOT NULL,
            owner_roster_id          INTEGER,
            previous_owner_roster_id INTEGER,
            PRIMARY KEY (league_id, season, round, original_roster_id)
        )
    """,
    # The waiver-wire universe. Must be derived per league, never globally.
    "available_players": """
        CREATE TABLE IF NOT EXISTS available_players (
            league_id  VARCHAR NOT NULL,
            sleeper_id VARCHAR NOT NULL,
            player_id  VARCHAR,   -- nflverse gsis_id, NULL when uncrosswalked
            full_name  VARCHAR,
            position   VARCHAR,
            team       VARCHAR,
            PRIMARY KEY (league_id, sleeper_id)
        )
    """,
    # User-set, never fetched. Deliberately its own table so re-linking a league
    # cannot wipe it — record and roster age hint at intent but guess wrong
    # often enough to produce confidently bad advice.
    "team_intent": """
        CREATE TABLE IF NOT EXISTS team_intent (
            league_id  VARCHAR NOT NULL,
            roster_id  INTEGER NOT NULL,
            intent     VARCHAR NOT NULL,  -- contend|rebuild|balanced
            updated_at TIMESTAMP NOT NULL,
            PRIMARY KEY (league_id, roster_id)
        )
    """,
    # Chat history for the web UI. User-owned like team_intent, so re-ingesting
    # stats or re-linking a league must never touch it. The HTTP layer is
    # stateless — every request reloads the thread from here — which is what
    # lets a page refresh keep the conversation.
    "conversations": """
        CREATE TABLE IF NOT EXISTS conversations (
            conversation_id VARCHAR PRIMARY KEY,
            league_id       VARCHAR NOT NULL,
            roster_id       INTEGER,
            title           VARCHAR,
            created_at      TIMESTAMP NOT NULL,
            updated_at      TIMESTAMP NOT NULL
        )
    """,
    "messages": """
        CREATE TABLE IF NOT EXISTS messages (
            conversation_id VARCHAR NOT NULL,
            seq             INTEGER NOT NULL,
            role            VARCHAR NOT NULL,  -- user|assistant
            content         VARCHAR NOT NULL,
            -- What the assistant called to produce this, for the UI's trace
            -- panel. Kept because "which numbers came from where" is the whole
            -- premise of the app.
            tools_used      VARCHAR,
            created_at      TIMESTAMP NOT NULL,
            PRIMARY KEY (conversation_id, seq)
        )
    """,
}

VIEWS: dict[str, str] = {
    "v_player_season_totals": f"""
        CREATE OR REPLACE VIEW v_player_season_totals AS
        SELECT
            s.player_id,
            s.season,
            ANY_VALUE(s.position)         AS position,
            COUNT(*)                      AS games,
            SUM(s.completions)            AS completions,
            SUM(s.attempts)               AS attempts,
            SUM(s.passing_yards)          AS passing_yards,
            SUM(s.passing_tds)            AS passing_tds,
            SUM(s.interceptions)          AS interceptions,
            SUM(s.carries)                AS carries,
            SUM(s.rushing_yards)          AS rushing_yards,
            SUM(s.rushing_tds)            AS rushing_tds,
            SUM(s.receptions)             AS receptions,
            SUM(s.targets)                AS targets,
            SUM(s.receiving_yards)        AS receiving_yards,
            SUM(s.receiving_tds)          AS receiving_tds,
            SUM(s.fumbles)                AS fumbles,
            SUM(s.fumbles_lost)           AS fumbles_lost,
            SUM(s.passing_first_downs)    AS passing_first_downs,
            SUM(s.rushing_first_downs)    AS rushing_first_downs,
            SUM(s.receiving_first_downs)  AS receiving_first_downs,
            SUM(s.passing_40)             AS passing_40,
            SUM(s.rushing_40)             AS rushing_40,
            SUM(s.receiving_40)           AS receiving_40,
            SUM(s.passing_2pt_conversions)   AS passing_2pt_conversions,
            SUM(s.rushing_2pt_conversions)   AS rushing_2pt_conversions,
            SUM(s.receiving_2pt_conversions) AS receiving_2pt_conversions,
            SUM(s.special_teams_tds)         AS special_teams_tds
        FROM player_week_stats s
        WHERE s.season_type = '{REGULAR_SEASON}'
        GROUP BY s.player_id, s.season
    """,
    # Trailing 3-game averages, one row per player-week. Uses ROWS (not RANGE)
    # so a missed week collapses to the previous three games played, which is
    # the read a human wants from "last 3 weeks".
    "v_player_rolling_3wk": f"""
        CREATE OR REPLACE VIEW v_player_rolling_3wk AS
        SELECT
            s.player_id,
            s.season,
            s.week,
            s.position,
            s.team,
            COUNT(*) OVER w                    AS games_in_window,
            AVG(s.passing_yards)   OVER w      AS avg_passing_yards,
            AVG(s.passing_tds)     OVER w      AS avg_passing_tds,
            AVG(s.carries)         OVER w      AS avg_carries,
            AVG(s.rushing_yards)   OVER w      AS avg_rushing_yards,
            AVG(s.rushing_tds)     OVER w      AS avg_rushing_tds,
            AVG(s.receptions)      OVER w      AS avg_receptions,
            AVG(s.targets)         OVER w      AS avg_targets,
            AVG(s.receiving_yards) OVER w      AS avg_receiving_yards,
            AVG(s.receiving_tds)   OVER w      AS avg_receiving_tds,
            AVG(u.snap_share)      OVER w      AS avg_snap_share,
            AVG(u.target_share)    OVER w      AS avg_target_share,
            AVG(u.red_zone_touches) OVER w     AS avg_red_zone_touches
        FROM player_week_stats s
        LEFT JOIN player_week_usage u
            ON  u.player_id   = s.player_id
            AND u.season      = s.season
            AND u.week        = s.week
            AND u.season_type = s.season_type
        WHERE s.season_type = '{REGULAR_SEASON}'
        WINDOW w AS (
            PARTITION BY s.player_id, s.season
            ORDER BY s.week
            ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
        )
    """,
    # How generous each defense is to each position, in raw production rather
    # than fantasy points (points are league-specific and cannot live in a
    # view). rank 1 = fewest yards allowed per game = toughest matchup.
    "v_position_defense_rank": f"""
        CREATE OR REPLACE VIEW v_position_defense_rank AS
        WITH allowed AS (
            SELECT
                s.season,
                s.opponent_team AS defense_team,
                s.position,
                COUNT(DISTINCT s.week) AS weeks,
                SUM(COALESCE(s.passing_yards, 0)
                    + COALESCE(s.rushing_yards, 0)
                    + COALESCE(s.receiving_yards, 0)) AS yards_allowed,
                SUM(COALESCE(s.passing_tds, 0)
                    + COALESCE(s.rushing_tds, 0)
                    + COALESCE(s.receiving_tds, 0))   AS tds_allowed
            FROM player_week_stats s
            WHERE s.season_type = '{REGULAR_SEASON}'
              AND s.opponent_team IS NOT NULL
              AND s.position IS NOT NULL
            GROUP BY s.season, s.opponent_team, s.position
        )
        SELECT
            season,
            defense_team,
            position,
            weeks,
            yards_allowed,
            tds_allowed,
            yards_allowed / NULLIF(weeks, 0) AS yards_allowed_per_game,
            tds_allowed   / NULLIF(weeks, 0) AS tds_allowed_per_game,
            RANK() OVER (
                PARTITION BY season, position
                ORDER BY yards_allowed / NULLIF(weeks, 0)
            ) AS defense_rank
        FROM allowed
    """,
}


def create_schema() -> None:
    """Create every table and view if it doesn't already exist. Idempotent."""
    for ddl in TABLES.values():
        query(ddl)
    for ddl in VIEWS.values():
        query(ddl)


def drop_schema() -> None:
    """Drop everything. Used by `make clean`-style resets and by tests."""
    for name in VIEWS:
        query(f"DROP VIEW IF EXISTS {name}")
    for name in TABLES:
        query(f"DROP TABLE IF EXISTS {name}")
