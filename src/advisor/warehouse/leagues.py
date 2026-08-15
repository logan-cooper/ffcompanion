"""Pull a Sleeper league's structure into the local database.

Idempotent per league in the same way the stats ingest is: every load deletes
the league's rows before rewriting them. The one exception is `team_intent`,
which is user-set rather than fetched and is never touched here.

All SQL goes through `db.query()` — this module never imports duckdb.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from advisor.db import query
from advisor.league_format import (
    DEFAULT_TEAM_INTENT,
    TEAM_INTENTS,
    FormatDetection,
    detect_format,
)
from advisor.sources import sleeper
from advisor.warehouse import account
from advisor.warehouse.schema import create_schema

# Rows per multi-row INSERT. The free-agent pool is ~2,900 players per league,
# and one statement per row would mean thousands of round trips.
INSERT_CHUNK_SIZE = 500


@dataclass
class LeagueSummary:
    league_id: str
    name: str
    season: int
    detection: FormatDetection
    rosters: int
    users: int
    rostered_players: int
    available_players: int
    traded_picks: int


def insert_many(
    table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]
) -> int:
    """Insert rows in chunks, binding every value."""
    rows = list(rows)
    if not rows:
        return 0

    column_sql = ", ".join(columns)
    placeholder = "(" + ", ".join("?" for _ in columns) + ")"

    for start in range(0, len(rows), INSERT_CHUNK_SIZE):
        chunk = rows[start : start + INSERT_CHUNK_SIZE]
        values_sql = ", ".join(placeholder for _ in chunk)
        params = [value for row in chunk for value in row]
        query(f"INSERT INTO {table} ({column_sql}) VALUES {values_sql}", params)

    return len(rows)


def _as_json(value: Any) -> str:
    return json.dumps(value if value is not None else [])


def _load_league(league: dict, season: int, detection: FormatDetection) -> None:
    league_id = league["league_id"]
    query("DELETE FROM leagues WHERE league_id = ?", [league_id])
    query(
        """
        INSERT INTO leagues (
            league_id, season, name, status, total_rosters, previous_league_id,
            sleeper_type, format, format_source, superflex, has_taxi,
            is_continuation, roster_positions, scoring_settings, settings,
            fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            league_id,
            season,
            (league.get("name") or "").strip(),
            league.get("status"),
            league.get("total_rosters"),
            league.get("previous_league_id"),
            detection.sleeper_type,
            detection.format,
            detection.source,
            detection.superflex,
            detection.has_taxi,
            detection.is_continuation,
            _as_json(league.get("roster_positions")),
            json.dumps(league.get("scoring_settings") or {}),
            json.dumps(league.get("settings") or {}),
            datetime.now(timezone.utc).replace(tzinfo=None),
        ],
    )


def _load_users(league_id: str, users: list[dict]) -> int:
    query("DELETE FROM league_users WHERE league_id = ?", [league_id])
    return insert_many(
        "league_users",
        ("league_id", "user_id", "display_name", "team_name"),
        [
            (
                league_id,
                u.get("user_id"),
                u.get("display_name"),
                (u.get("metadata") or {}).get("team_name"),
            )
            for u in users
        ],
    )


def _load_rosters(league_id: str, rosters: list[dict]) -> int:
    query("DELETE FROM league_rosters WHERE league_id = ?", [league_id])
    return insert_many(
        "league_rosters",
        (
            "league_id", "roster_id", "owner_id", "players", "starters",
            "taxi", "reserve", "wins", "losses", "ties", "fpts",
        ),
        [
            (
                league_id,
                r.get("roster_id"),
                r.get("owner_id"),
                _as_json(r.get("players")),
                _as_json(r.get("starters")),
                _as_json(r.get("taxi")),
                _as_json(r.get("reserve")),
                (r.get("settings") or {}).get("wins"),
                (r.get("settings") or {}).get("losses"),
                (r.get("settings") or {}).get("ties"),
                (r.get("settings") or {}).get("fpts"),
            )
            for r in rosters
        ],
    )


def _load_traded_picks(league_id: str, picks: list[dict]) -> int:
    query("DELETE FROM traded_picks WHERE league_id = ?", [league_id])
    rows = []
    seen = set()
    for p in picks:
        try:
            season = int(p["season"])
            round_ = int(p["round"])
            original = int(p["roster_id"])
        except (KeyError, TypeError, ValueError):
            continue
        # A pick can be traded repeatedly; only the current holder matters, and
        # Sleeper lists the chain. Keep the first (most recent) per pick.
        key = (season, round_, original)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            (
                league_id,
                season,
                round_,
                original,
                p.get("owner_id"),
                p.get("previous_owner_id"),
            )
        )
    return insert_many(
        "traded_picks",
        (
            "league_id", "season", "round", "original_roster_id",
            "owner_roster_id", "previous_owner_roster_id",
        ),
        rows,
    )


def _load_available_players(
    league_id: str,
    rosters: list[dict],
    pool: dict[str, dict],
    season: int | None = None,
) -> int:
    """Everyone in the fantasy pool not on a roster in *this* league.

    Derived per league, never globally — a player who is a free agent in a
    10-team league may be rostered in a 16-teamer.
    """
    query("DELETE FROM available_players WHERE league_id = ?", [league_id])

    rostered: set[str] = set()
    for roster in rosters:
        for key in ("players", "taxi", "reserve"):
            for pid in roster.get(key) or []:
                rostered.add(str(pid))

    inserted = insert_many(
        "available_players",
        ("league_id", "sleeper_id", "player_id", "full_name", "position", "team"),
        [
            (
                league_id,
                pid,
                p.get("gsis_id"),
                p.get("full_name"),
                p.get("position"),
                p.get("team"),
            )
            for pid, p in pool.items()
            if pid not in rostered
        ],
    )

    # Sleeper's own gsis_id is null for a large share of players, including real
    # contributors. The stats warehouse carries the crosswalk in the other
    # direction (nflverse rosters publish sleeper_id), so fill the gaps from it.
    # Without this, a free agent that can't be joined to stats is invisible to
    # any tool that ranks the waiver wire by production.
    if season is not None:
        query(
            """
            UPDATE available_players AS a
            SET player_id = p.player_id
            FROM players AS p
            WHERE a.league_id = ?
              AND a.player_id IS NULL
              AND p.season = ?
              AND p.sleeper_id = a.sleeper_id
            """,
            [league_id, season],
        )

    return inserted


def set_team_intent(league_id: str, roster_id: int, intent: str) -> None:
    """Record how a team is playing the season. User-set; never inferred."""
    if intent not in TEAM_INTENTS:
        raise ValueError(f"intent must be one of {TEAM_INTENTS}, got {intent!r}")
    query(
        "DELETE FROM team_intent WHERE league_id = ? AND roster_id = ?",
        [league_id, roster_id],
    )
    query(
        "INSERT INTO team_intent (league_id, roster_id, intent, updated_at) "
        "VALUES (?, ?, ?, ?)",
        [league_id, roster_id, intent, datetime.now(timezone.utc).replace(tzinfo=None)],
    )


def get_team_intent(league_id: str, roster_id: int) -> str:
    rows = query(
        "SELECT intent FROM team_intent WHERE league_id = ? AND roster_id = ?",
        [league_id, roster_id],
    )
    return rows[0]["intent"] if rows else DEFAULT_TEAM_INTENT


def ingest_league(
    league: dict,
    season: int,
    pool: dict[str, dict],
    *,
    users: list[dict] | None = None,
    rosters: list[dict] | None = None,
    picks: list[dict] | None = None,
) -> LeagueSummary:
    """Load one already-fetched league object and everything hanging off it.

    `users`/`rosters`/`picks` can be supplied by a caller that already has them.
    The refresh path fetches all four in parallel to decide whether anything
    changed; without this it would then fetch three of them a second time to do
    the write.
    """
    league_id = league["league_id"]
    detection = detect_format(league)

    if users is None:
        users = sleeper.get_league_users(league_id)
    if rosters is None:
        rosters = sleeper.get_rosters(league_id)
    if picks is None:
        picks = sleeper.get_traded_picks(league_id)

    _load_league(league, season, detection)
    n_users = _load_users(league_id, users)
    n_rosters = _load_rosters(league_id, rosters)
    n_picks = _load_traded_picks(league_id, picks)
    n_available = _load_available_players(league_id, rosters, pool, season)

    rostered = sum(len(r.get("players") or []) for r in rosters)

    return LeagueSummary(
        league_id=league_id,
        name=(league.get("name") or "").strip(),
        season=season,
        detection=detection,
        rosters=n_rosters,
        users=n_users,
        rostered_players=rostered,
        available_players=n_available,
        traded_picks=n_picks,
    )


def link_leagues(
    username: str,
    season: int,
    *,
    league_id: str | None = None,
    refresh: bool = False,
) -> tuple[str, list[LeagueSummary]]:
    """Link every league `username` played in that season. Returns (user_id, summaries)."""
    create_schema()

    user = sleeper.get_user(username)
    if not user:
        raise LookupError(f"no Sleeper user named {username!r}")
    user_id = user["user_id"]

    # Record who "you" are while Sleeper has just told us. Roster ownership is
    # not derivable from league data — league_users lists every manager and
    # marks none of them as the person asking — so without this, linking
    # succeeds and "how does my roster look?" still has no subject. Store
    # Sleeper's display_name rather than what was typed: a user id also
    # resolves through this endpoint, and league_users holds display names.
    account.set_account(user.get("display_name") or username, user_id)

    leagues = sleeper.get_user_leagues(user_id, season)
    if league_id:
        leagues = [lg for lg in leagues if lg["league_id"] == league_id]
        if not leagues:
            raise LookupError(f"{username} has no league {league_id} in {season}")
    if not leagues:
        raise LookupError(f"{username} has no NFL leagues in {season}")

    pool = sleeper.fantasy_player_pool(sleeper.get_all_players(refresh=refresh))
    summaries = [ingest_league(lg, season, pool) for lg in leagues]

    # First link is where the player mirror gets built. Imported here rather
    # than on a page load because it costs seconds, and linking is already a
    # deliberate wait.
    from advisor.warehouse.refresh import sync_players

    sync_players()

    return user_id, summaries
