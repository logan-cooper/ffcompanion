"""Phase 0 smoke tests: the skeleton imports, reports a version, and can talk
to a database through the repository layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from advisor import __version__, cli
from advisor.config import PROJECT_ROOT, Settings
from advisor.db import query


def test_version_is_populated():
    assert __version__
    assert __version__ != "0.0.0+unknown"


def test_cli_version_flag_prints_version(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])

    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_relative_paths_resolve_against_project_root():
    settings = Settings(db_path=Path("data/advisor.duckdb"))

    assert settings.db_path == PROJECT_ROOT / "data" / "advisor.duckdb"


def test_query_round_trips_through_the_repository_layer(temp_db):
    query("CREATE TABLE probe (id INTEGER, name TEXT)")
    query("INSERT INTO probe VALUES (?, ?)", [1, "Bijan"])

    assert query("SELECT * FROM probe WHERE id = ?", [1]) == [{"id": 1, "name": "Bijan"}]


def test_statements_without_a_result_set_return_empty(temp_db):
    assert query("CREATE TABLE empty_probe (id INTEGER)") == []
