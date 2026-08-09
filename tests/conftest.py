from __future__ import annotations

from pathlib import Path

import pytest

from advisor import db


@pytest.fixture
def temp_db(tmp_path: Path):
    """Point the repository layer at a throwaway database file."""
    db.close_conn()
    db.get_conn(tmp_path / "test.duckdb")
    yield
    db.close_conn()
