"""Thin typed client for the Sleeper API.

Free, read-only, no auth token. The documented courtesy limit is ~1000 requests
per minute; linking a league costs well under ten, so the only real concern is
flakiness — every call retries with backoff.

Nothing here touches the database. Callers get plain dicts and lists.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import requests

from advisor.config import get_settings

log = logging.getLogger(__name__)

BASE_URL = "https://api.sleeper.app/v1"

# The full player dump is ~14MB and changes slowly; anything else is cheap.
PLAYERS_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 1.5
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
TIMEOUT_SECONDS = 30

FANTASY_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DEF"})


class SleeperError(RuntimeError):
    """A Sleeper request failed after exhausting retries."""


def _get(path: str) -> Any:
    """GET `path` under the API base, retrying transient failures.

    Sleeper returns `null` rather than 404 for unknown users and leagues, so a
    None result is a legitimate "not found" and is passed through.
    """
    url = f"{BASE_URL}/{path.lstrip('/')}"
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.get(url, timeout=TIMEOUT_SECONDS)
            if response.status_code in RETRY_STATUS:
                raise SleeperError(f"{response.status_code} from {url}")
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, SleeperError, ValueError) as exc:
            last_error = exc
            if attempt == MAX_ATTEMPTS:
                break
            delay = BACKOFF_SECONDS * (2 ** (attempt - 1))
            log.warning(
                "sleeper %s failed (attempt %d/%d): %s; retrying in %.1fs",
                path,
                attempt,
                MAX_ATTEMPTS,
                exc,
                delay,
            )
            time.sleep(delay)

    raise SleeperError(f"GET {url} failed after {MAX_ATTEMPTS} attempts: {last_error}")


def get_user(username: str) -> dict | None:
    """Resolve a username (or user id) to a user object. None if unknown."""
    return _get(f"user/{username}")


def get_user_leagues(user_id: str, season: int) -> list[dict]:
    return _get(f"user/{user_id}/leagues/nfl/{season}") or []


def get_league(league_id: str) -> dict | None:
    return _get(f"league/{league_id}")


def get_rosters(league_id: str) -> list[dict]:
    return _get(f"league/{league_id}/rosters") or []


def get_league_users(league_id: str) -> list[dict]:
    return _get(f"league/{league_id}/users") or []


def get_matchups(league_id: str, week: int) -> list[dict]:
    return _get(f"league/{league_id}/matchups/{week}") or []


def get_transactions(league_id: str, week: int) -> list[dict]:
    return _get(f"league/{league_id}/transactions/{week}") or []


def get_traded_picks(league_id: str) -> list[dict]:
    """Future draft picks that have changed hands.

    Dynasty-critical: picks are tradeable assets, and a large share of real
    dynasty proposals involve them.
    """
    return _get(f"league/{league_id}/traded_picks") or []


def get_trending_players(kind: str = "add", lookback_hours: int = 24, limit: int = 25) -> list[dict]:
    """Waiver signal: [{player_id, count}], most-added first."""
    return (
        _get(
            f"players/nfl/trending/{kind}"
            f"?lookback_hours={lookback_hours}&limit={limit}"
        )
        or []
    )


def _players_cache_path() -> Path:
    cache_dir = get_settings().cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "sleeper_players.json"


def get_all_players(*, refresh: bool = False) -> dict[str, dict]:
    """The full NFL player dump, keyed by Sleeper player_id.

    ~14MB and ~12k players, so it is cached to disk and refreshed weekly. This
    is the universe `available_players` is derived from.
    """
    path = _players_cache_path()

    if path.exists() and not refresh:
        age = time.time() - path.stat().st_mtime
        if age < PLAYERS_CACHE_MAX_AGE_SECONDS:
            return json.loads(path.read_text())

    players = _get("players/nfl")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(players))
    tmp.replace(path)
    return players


def fantasy_player_pool(players: dict[str, dict]) -> dict[str, dict]:
    """Narrow the dump to players a fantasy league could actually roster."""
    return {
        pid: p
        for pid, p in players.items()
        if p.get("position") in FANTASY_POSITIONS
        and (p.get("active") or p.get("status") == "Active")
    }
