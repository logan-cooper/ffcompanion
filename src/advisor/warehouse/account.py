"""Which Sleeper account is "you".

User-set data, so it gets the same treatment as `team_intent`: its own table,
never inferred, and untouched by any re-ingest. Re-linking a league must not
make the app forget whose roster it is looking at.

Stored here rather than only in `.env` because the web UI can set it at runtime.
`get_settings()` is `lru_cache`d, so an env-only value does not take effect
until the process restarts — an unreasonable thing to require of someone who
just typed their name into a box and pressed a button.

Imports only `db` and the DDL, deliberately: `context.list_leagues()` reads this
on every league lookup, and pulling `sources.sleeper` (and therefore `requests`)
into that path buys nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from advisor.db import query
from advisor.warehouse.schema import TABLES


def _ensure() -> None:
    """Create the table if this warehouse predates it.

    Every warehouse built before this feature has no `sleeper_account`, and a
    missing table would fail `list_leagues()` — that is, every league lookup in
    both interfaces. The DDL is `IF NOT EXISTS` and comes from schema.py, so
    this is a cheap no-op with one definition, not a second copy.
    """
    query(TABLES["sleeper_account"])


def get_account() -> dict[str, Any] | None:
    """The recorded Sleeper identity, or None if the app has never been told."""
    _ensure()
    rows = query(
        "SELECT username, user_id, linked_at FROM sleeper_account "
        "ORDER BY linked_at DESC LIMIT 1"
    )
    return rows[0] if rows else None


def set_account(username: str, user_id: str | None = None) -> None:
    """Record who "you" are. Replaces rather than accumulates — one manager.

    Pass Sleeper's `display_name`, not whatever was typed: `league_users` stores
    the display name, and that is what roster ownership is matched against.
    """
    username = username.strip()
    if not username:
        raise ValueError("username must not be empty")

    _ensure()
    query("DELETE FROM sleeper_account")
    query(
        "INSERT INTO sleeper_account (username, user_id, linked_at) VALUES (?, ?, ?)",
        [username, user_id, datetime.now(timezone.utc).replace(tzinfo=None)],
    )


def clear_account() -> None:
    _ensure()
    query("DELETE FROM sleeper_account")
