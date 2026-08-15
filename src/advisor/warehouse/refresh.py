"""Pull a league's current state from Sleeper, cheaply enough to do on every load.

Rosters move on every trade and every waiver claim. A warehouse updated only by
the weekly `make refresh` gives advice about a team the user no longer has —
recommending a start for a player they dropped on Tuesday. This module is what
makes "the app opens on today's roster" true, and it covers exactly the data
Sleeper owns: rosters, managers, traded picks, and the free-agent pool derived
from them. Player *stats* come from nflverse and still publish weekly.

Three things keep it cheap enough to sit in the page-load path:

1. **The Sleeper calls run in parallel.** They are independent and each is a
   ~0.4s round trip; run in sequence they were the entire cost.
2. **Nothing is written when nothing changed.** The fingerprint covers what we
   would *store*, not what was received, so an unchanged league — which is most
   leagues, most of the time — skips the rewrite entirely. That also keeps
   `available_players` from being briefly empty underneath a reader.
3. **Everything is fetched before anything is written.** A failure part-way
   leaves the warehouse exactly as it was, which is what lets the app keep
   working with no network at all.

A failure here is never fatal. The warehouse copy is a complete, working answer;
it is just older, and callers say so rather than breaking.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from advisor.db import query
from advisor.sources import sleeper
from advisor.sources.sleeper import SleeperError
from advisor.warehouse.leagues import ingest_league, insert_many
from advisor.warehouse.schema import TABLES

log = logging.getLogger(__name__)

# A result this fresh is handed to a second caller instead of re-fetching. Two
# tabs opening together, or a double refresh, is one burst and not two — this is
# deduplication, not a staleness budget.
SHARE_SECONDS = 5.0

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_recent: dict[str, tuple[float, "RefreshResult"]] = {}


@dataclass
class RefreshResult:
    league_id: str
    ok: bool
    changed: bool
    reason: str | None = None
    rosters: int = 0
    available_players: int = 0
    synced_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ensure() -> None:
    """Self-healing: every warehouse built before this feature lacks the table."""
    query(TABLES["league_sync"])


def _lock_for(league_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(league_id, threading.Lock())


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sync_players(*, force: bool = False) -> int:
    """Mirror Sleeper's full player dump into the warehouse. Returns rows written.

    Stored **unfiltered**, unlike the fantasy pool `available_players` is built
    from. `players` comes from nflverse and only lists people on an NFL roster
    that season, so a taxi-squad stash or a player out of the league had no name
    anywhere — and showed up on his own manager's roster as "(unknown player)".

    Skipped entirely when the mirror is already newer than the cached dump, so
    the common case is two cheap reads and no 14MB parse.
    """
    query(TABLES["sleeper_players"])

    path = sleeper._players_cache_path()
    cached_at = (
        datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(tzinfo=None)
        if path.exists()
        else None
    )
    rows = query("SELECT MAX(fetched_at) AS at, COUNT(*) AS n FROM sleeper_players")
    stored_at, stored_n = rows[0]["at"], rows[0]["n"]

    if not force and stored_n and cached_at and stored_at and stored_at >= cached_at:
        return 0

    try:
        dump = sleeper.get_all_players()
    except SleeperError as exc:
        # Never fatal: an out-of-date mirror still names almost everybody.
        log.warning("player dump unavailable: %s", exc)
        return 0

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    records = [
        (
            str(sleeper_id),
            player.get("gsis_id"),
            player.get("full_name")
            or " ".join(
                filter(None, [player.get("first_name"), player.get("last_name")])
            )
            or None,
            player.get("position"),
            player.get("team"),
            player.get("status"),
            player.get("injury_status"),
            _as_int(player.get("age")),
            _as_int(player.get("years_exp")),
            now,
        )
        for sleeper_id, player in dump.items()
    ]

    query("DELETE FROM sleeper_players")
    written = insert_many(
        "sleeper_players",
        ("sleeper_id", "player_id", "full_name", "position", "team", "status",
         "injury_status", "age", "years_exp", "fetched_at"),
        records,
    )
    log.info("mirrored %d Sleeper players", written)
    return written


def identify(sleeper_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Who these Sleeper ids are: name, position, team, status.

    An answer for any rostered player, whether or not the stats warehouse has
    ever seen them. Three sources in order, each a fallback for the last:

    1. `sleeper_players`, the mirror — one query, everyone.
    2. `players`, the nflverse crosswalk.
    3. The cached dump on disk, parsed.

    The third exists so correctness never waits on the mirror. Importing 12k
    rows takes seconds and only happens on paths where seconds are already being
    spent, so on a fresh install — or the week the dump updates — the mirror is
    behind, and a manager should still see his own player's name.
    """
    if not sleeper_ids:
        return {}

    query(TABLES["sleeper_players"])
    wanted = [str(s) for s in sleeper_ids]
    placeholders = ", ".join("?" for _ in wanted)

    found = {
        row["sleeper_id"]: row
        for row in query(
            f"""
            SELECT sleeper_id, full_name, position, team, status, injury_status,
                   age, years_exp
            FROM sleeper_players WHERE sleeper_id IN ({placeholders})
            """,
            wanted,
        )
    }

    missing = [s for s in wanted if s not in found]
    if missing:
        placeholders = ", ".join("?" for _ in missing)
        for row in query(
            f"""
            SELECT sleeper_id, ANY_VALUE(full_name) AS full_name,
                   ANY_VALUE(position) AS position, ANY_VALUE(team) AS team
            FROM players
            WHERE sleeper_id IN ({placeholders})
            GROUP BY sleeper_id
            """,
            missing,
        ):
            found[row["sleeper_id"]] = row

    missing = [s for s in wanted if s not in found]
    if missing:
        for sleeper_id, player in _from_cached_dump(missing).items():
            found[sleeper_id] = player

    return found


def _from_cached_dump(sleeper_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Read identities straight out of the cached dump. ~0.08s, no network."""
    try:
        dump = sleeper.get_all_players()
    except SleeperError as exc:
        log.warning("cannot identify %d players: %s", len(sleeper_ids), exc)
        return {}

    out = {}
    for sleeper_id in sleeper_ids:
        player = dump.get(sleeper_id)
        if not player:
            continue
        out[sleeper_id] = {
            "sleeper_id": sleeper_id,
            "full_name": player.get("full_name"),
            "position": player.get("position"),
            "team": player.get("team"),
            "status": player.get("status"),
            "injury_status": player.get("injury_status"),
            "age": _as_int(player.get("age")),
            "years_exp": _as_int(player.get("years_exp")),
        }
    return out


def _fingerprint(league: dict, users: list, rosters: list, picks: list) -> str:
    """Hash the fields we persist, so "unchanged" means "the write is a no-op".

    Hashing the raw responses instead would catch volatile fields Sleeper
    updates constantly — a last-read marker moving would look like a trade — and
    every refresh would rewrite everything, which is the cost this exists to
    avoid.
    """
    payload = {
        "league": {
            key: league.get(key)
            for key in (
                "name", "status", "total_rosters", "previous_league_id",
                "roster_positions", "scoring_settings", "settings",
            )
        },
        "users": sorted(
            (
                u.get("user_id"),
                u.get("display_name"),
                (u.get("metadata") or {}).get("team_name"),
            )
            for u in users
        ),
        "rosters": sorted(
            (
                r.get("roster_id"),
                r.get("owner_id"),
                json.dumps(r.get("players") or [], sort_keys=True),
                json.dumps(r.get("starters") or [], sort_keys=True),
                json.dumps(r.get("taxi") or [], sort_keys=True),
                json.dumps(r.get("reserve") or [], sort_keys=True),
                json.dumps(
                    {
                        k: (r.get("settings") or {}).get(k)
                        for k in ("wins", "losses", "ties", "fpts")
                    },
                    sort_keys=True,
                ),
            )
            for r in rosters
        ),
        "picks": sorted(
            (p.get("season"), p.get("round"), p.get("roster_id"),
             p.get("owner_id"), p.get("previous_owner_id"))
            for p in picks
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _stored_fingerprint(league_id: str) -> str | None:
    rows = query(
        "SELECT fingerprint FROM league_sync WHERE league_id = ?", [league_id]
    )
    return rows[0]["fingerprint"] if rows else None


def _looks_loaded(league_id: str) -> bool:
    """Is the data the fingerprint claims to describe actually there?

    The fingerprint says "a rewrite would be a no-op", which is only true if the
    previous write finished. An ingest interrupted between the DELETE and the
    INSERT would otherwise leave an empty waiver wire that every later refresh
    politely skips — stuck, with nothing saying why.
    """
    rows = query(
        """
        SELECT (SELECT COUNT(*) FROM league_rosters WHERE league_id = ?) AS rosters,
               (SELECT COUNT(*) FROM available_players WHERE league_id = ?) AS wire
        """,
        [league_id, league_id],
    )
    return bool(rows and rows[0]["rosters"] and rows[0]["wire"])


def _record(league_id: str, fingerprint: str) -> str:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    query("DELETE FROM league_sync WHERE league_id = ?", [league_id])
    query(
        "INSERT INTO league_sync (league_id, fingerprint, synced_at) "
        "VALUES (?, ?, ?)",
        [league_id, fingerprint, now],
    )
    return now.strftime("%Y-%m-%d %H:%M UTC")


def last_synced(league_id: str) -> str | None:
    """When this league was last confirmed current against Sleeper."""
    _ensure()
    rows = query(
        "SELECT synced_at FROM league_sync WHERE league_id = ?", [league_id]
    )
    if not rows or rows[0]["synced_at"] is None:
        return None
    return rows[0]["synced_at"].strftime("%Y-%m-%d %H:%M UTC")


def _fetch(league_id: str) -> tuple[dict, list, list, list]:
    """Everything Sleeper knows about this league, in parallel.

    Four independent round trips. Run in sequence they were ~1.5s of a ~1.6s
    refresh, which is the difference between this being page-load work and not.
    """
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            "league": pool.submit(sleeper.get_league, league_id),
            "users": pool.submit(sleeper.get_league_users, league_id),
            "rosters": pool.submit(sleeper.get_rosters, league_id),
            "picks": pool.submit(sleeper.get_traded_picks, league_id),
        }
        got = {name: future.result() for name, future in futures.items()}

    return got["league"], got["users"], got["rosters"], got["picks"]


def refresh_league(league_id: str, *, force: bool = False) -> RefreshResult:
    """Bring one league's rosters and free-agent pool up to date.

    Safe to call on every page load. Safe to call with no network. Never
    raises — a refresh that could not happen is a fact about the data, reported,
    not an error that takes the app down with it.

    `force` skips the share window and asks Sleeper again. It does **not** force
    a rewrite: the fingerprint still decides that, so `changed` always means the
    data actually moved rather than "we wrote it out anyway".
    """
    _ensure()

    now = time.monotonic()
    shared = _recent.get(league_id)
    if shared and not force and now - shared[0] < SHARE_SECONDS:
        return shared[1]

    with _lock_for(league_id):
        # Re-checked inside the lock: whoever we queued behind has just done
        # this, and doing it again immediately is the duplicate work the lock
        # was taken to prevent.
        shared = _recent.get(league_id)
        if shared and not force and time.monotonic() - shared[0] < SHARE_SECONDS:
            return shared[1]

        result = _refresh(league_id)
        _recent[league_id] = (time.monotonic(), result)
        return result


def _refresh(league_id: str) -> RefreshResult:
    rows = query("SELECT season FROM leagues WHERE league_id = ?", [league_id])
    if not rows:
        return RefreshResult(
            league_id, ok=False, changed=False,
            reason=f"league {league_id} is not linked",
        )
    season = rows[0]["season"]

    try:
        league, users, rosters, picks = _fetch(league_id)
    except SleeperError as exc:
        log.warning("refresh of %s failed: %s", league_id, exc)
        return RefreshResult(
            league_id, ok=False, changed=False,
            reason="Sleeper is unreachable — showing saved rosters",
            synced_at=last_synced(league_id),
        )

    # Sleeper answers `null` rather than 404 for a league that no longer exists.
    # Treating that as data would delete a working league and write nothing back.
    if not league:
        return RefreshResult(
            league_id, ok=False, changed=False,
            reason="Sleeper no longer returns this league — showing saved rosters",
            synced_at=last_synced(league_id),
        )

    fingerprint = _fingerprint(league, users, rosters, picks)
    if fingerprint == _stored_fingerprint(league_id) and _looks_loaded(league_id):
        return RefreshResult(
            league_id, ok=True, changed=False,
            rosters=len(rosters),
            synced_at=_record(league_id, fingerprint),
        )

    pool = sleeper.fantasy_player_pool(sleeper.get_all_players())
    summary = ingest_league(
        league, season, pool, users=users, rosters=rosters, picks=picks
    )

    # Only on the path that was already writing. Importing 12k rows costs
    # seconds, and it self-skips unless the dump is newer than the mirror, so
    # the common page load never touches it — and `identify` reads the dump
    # directly meanwhile, so nothing waits on this to be right.
    sync_players()

    return RefreshResult(
        league_id,
        ok=True,
        changed=True,
        rosters=summary.rosters,
        available_players=summary.available_players,
        synced_at=_record(league_id, fingerprint),
    )
