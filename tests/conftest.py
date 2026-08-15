from __future__ import annotations

from pathlib import Path

import pytest

from advisor import db
from advisor.config import get_settings

WAREHOUSE_SEASON = 2025


@pytest.fixture(autouse=True)
def _drop_memoised_database_state():
    """Caches that are really database state, dropped between tests.

    `players._available_seasons` and `tools.base._index_cache` are module-level
    and keyed on nothing that changes when the connection is repointed, so a
    test that swaps databases leaves the next one reading the previous
    database's answer. That is not hypothetical: a temp database with no stats
    poisoned `latest_season_with_data`, and a later test against the real
    warehouse then saw `stats_season: None` — but only when run after it, which
    is the worst way to find out.
    """
    from advisor import players
    from advisor.tools import base

    players.clear_caches()
    base.clear_index_cache()
    yield


@pytest.fixture
def temp_db(tmp_path: Path):
    """Point the repository layer at a throwaway database file."""
    db.close_conn()
    db.get_conn(tmp_path / "test.duckdb")
    yield
    db.close_conn()


@pytest.fixture
def warehouse():
    """Point at the real ingested warehouse, skipping if it isn't built.

    These assertions are about ingested data, so on a fresh clone they skip with
    instructions rather than fail.
    """
    db.close_conn()
    db_path = get_settings().db_path
    reason = f"no warehouse at {db_path}; run: make ingest SEASON={WAREHOUSE_SEASON}"

    if not db_path.exists():
        pytest.skip(reason)

    db.get_conn(db_path)
    try:
        rows = db.query(
            "SELECT COUNT(*) AS n FROM player_week_stats WHERE season = ?",
            [WAREHOUSE_SEASON],
        )
    except Exception:  # table missing entirely
        db.close_conn()
        pytest.skip(reason)

    if not rows[0]["n"]:
        db.close_conn()
        pytest.skip(reason)

    yield
    db.close_conn()


@pytest.fixture
def linked_leagues():
    """Point at real Sleeper leagues, skipping if none have been linked."""
    db.close_conn()
    db_path = get_settings().db_path
    reason = "no linked leagues; run: make link-league USERNAME=<you> SEASON=2025"

    if not db_path.exists():
        pytest.skip(reason)

    db.get_conn(db_path)
    try:
        rows = db.query("SELECT COUNT(*) AS n FROM leagues")
    except Exception:  # table missing entirely
        db.close_conn()
        pytest.skip(reason)

    if not rows[0]["n"]:
        db.close_conn()
        pytest.skip(reason)

    yield
    db.close_conn()
