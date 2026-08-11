# CLAUDE.md

## Project
Fantasy football AI advisor. Full plan: docs/ROADMAP.md — read it before
starting any phase.

**Status: Phase 1 complete** (skeleton + 2025 stats warehouse). Phase 2
(Sleeper league ingestion) is next. Don't skip ahead — each phase has a
"Done when" test that gates the next one.

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
- nflverse column names are NOT Sleeper stat keys. The mapping lives in
  src/advisor/warehouse/ingest.py (ingestion) and scoring/ (Phase 3); it is the
  most likely source of subtle wrong-number bugs. Notably: passing_interceptions
  -> interceptions, and fumbles_lost is the sum of sack/rushing/receiving
  fumbles lost.
- Capture dynasty fields (age, draft position, experience) at ingest even for
  redraft. They cannot be backfilled without re-ingesting.
- The warehouse holds 2023-2025, because one season misreads any player who was
  injured. Prior seasons are CONTEXT: keep them labeled by season and never
  blend them into a single average or into "recent form". Always return `games`
  alongside season totals — it's what separates "declined" from "was hurt".

## Commands
make sync                 # create .venv, install deps (uv)
make test
make warehouse            # build the full warehouse, 2023-2025, one command
make ingest SEASON=2025   # single season (idempotent, cached)
make status               # what's ingested and when
make chat                 # local CLI — Phase 5, not built yet
make eval                 # Phase 6, not built yet

Unbuilt targets exit 1 with the phase they land in; that's expected, not a
broken setup. Everything runs through `uv run` — don't call bare `python`
(system python is 3.9).