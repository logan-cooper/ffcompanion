# CLAUDE.md

## Project
Fantasy football AI advisor. Full plan: docs/ROADMAP.md — read it before
starting any phase.

**Status: Phase 0 complete** (skeleton). Phase 1 is next. Don't skip ahead —
each phase has a "Done when" test that gates the next one.

## Stack
Python 3.12, uv for deps, DuckDB locally (Postgres in Phase 8), FastAPI
(Phase 7+), Anthropic SDK for the agent loop.

## Hard rules
- All database access goes through src/advisor/db.py, which exposes exactly
  get_conn() and query() — no direct DuckDB imports anywhere else. query()
  handles writes too and returns [] when there's no result set.
- Tools (src/advisor/tools/) return data only, never prose or opinions.
- Raw stat tables store counting stats only — no fantasy_points column.
  Scoring is computed per-league at query time (src/advisor/scoring/).
- Every tool takes league_id and returns a data_as_of field.

## Commands
make sync                 # create .venv, install deps (uv)
make test
make ingest SEASON=2025   # populate local warehouse — Phase 1, not built yet
make chat                 # local CLI — Phase 5, not built yet
make eval                 # Phase 6, not built yet

Unbuilt targets exit 1 with the phase they land in; that's expected, not a
broken setup. Everything runs through `uv run` — don't call bare `python`
(system python is 3.9).