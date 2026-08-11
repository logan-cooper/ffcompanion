from __future__ import annotations

from pathlib import Path

import pytest

from advisor import db
from advisor.config import get_settings

WAREHOUSE_SEASON = 2025


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
