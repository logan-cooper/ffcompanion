"""Repository layer — the only module that knows which database we're using.

Every later phase talks to the database through `get_conn()` and `query()` and
imports nothing from `duckdb` directly. That discipline is what makes the
Phase 8 swap to Postgres a change to this one file.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import duckdb

from advisor.config import get_settings

Params = Sequence[Any] | Mapping[str, Any] | None

_conn: duckdb.DuckDBPyConnection | None = None
_lock = threading.Lock()


def get_conn(db_path: Path | str | None = None) -> duckdb.DuckDBPyConnection:
    """Return the shared database connection, opening it on first use.

    Pass `db_path` to open a specific file (tests use this); otherwise the path
    comes from settings. The parent directory is created if missing. The first
    call wins — call `close_conn()` before pointing at a different file.
    """
    global _conn

    with _lock:
        if _conn is None:
            path = Path(db_path) if db_path is not None else get_settings().db_path
            if path != Path(":memory:"):
                path.parent.mkdir(parents=True, exist_ok=True)
            _conn = duckdb.connect(str(path))
        return _conn


def query(sql: str, params: Params = None) -> list[dict[str, Any]]:
    """Run `sql` and return rows as dicts.

    Handles both reads and writes: statements that return no result set (DDL,
    INSERT, and friends) come back as an empty list. Parameters are bound, never
    interpolated — pass a sequence for `?` placeholders or a mapping for `$name`.
    """
    cursor = get_conn().cursor()
    try:
        if params is None:
            cursor.execute(sql)
        else:
            cursor.execute(sql, params)
        if cursor.description is None:
            return []
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def close_conn() -> None:
    """Close the shared connection. Mainly for tests and clean shutdown."""
    global _conn

    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
