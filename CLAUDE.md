# CLAUDE.md

## Project
Fantasy football AI advisor. Full plan: docs/ROADMAP.md — read it before
starting any phase.

**Status: Phase 3 complete** (warehouse 2023-2025, Sleeper leagues, scoring
engine). Phase 3b (format-aware valuation) is next. Don't skip ahead — each
phase has a "Done when" test that gates the next one.

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
- nflverse column names are NOT Sleeper stat keys. The mapping lives in ONE
  place, src/advisor/scoring/keys.py, and is the most likely source of subtle
  wrong-number bugs. After editing it run `make verify-scoring`, which diffs
  every warehouse stat line against the points Sleeper actually recorded
  (~10k player-weeks, currently 98.98% exact). Unit tests over hand-written
  stat lines will NOT catch a bad mapping.
- A touchdown is a first down to nflverse but NOT to Sleeper. Any first-down
  scoring key must subtract touchdowns. This was 744 wrong player-weeks in one
  league before it was caught.
- Capture dynasty fields (age, draft position, experience) at ingest even for
  redraft. They cannot be backfilled without re-ingesting.
- The warehouse holds 2023-2025, because one season misreads any player who was
  injured. Prior seasons are CONTEXT: keep them labeled by season and never
  blend them into a single average or into "recent form". Always return `games`
  alongside season totals — it's what separates "declined" from "was hurt".
- Format lives in src/advisor/league_format.py and is READ from the league,
  never guessed. Verified Sleeper settings.type: 2=dynasty, 3=survival.
  0=redraft and 1=keeper are documented-only (fixture-tested; this account has
  no redraft league on Sleeper). Anything ambiguous is `unknown`, which means
  ASK the user — never default to a format.
- team_intent is user-set, never inferred, and lives in its own table so
  re-linking a league can't wipe it. Same rule for any future user-set data.
- Sleeper<->nflverse crosswalk goes BOTH ways: Sleeper's gsis_id is null for
  many real contributors, so available_players backfills player_id from
  players.sleeper_id. Never rely on one direction alone.

## Commands
make sync                 # create .venv, install deps (uv)
make test
make warehouse            # build the full warehouse, 2023-2025, one command
make ingest SEASON=2025   # single season (idempotent, cached)
make status               # what's ingested and when
make link-league USERNAME=cooper257 SEASON=2025
make chat                 # local CLI — Phase 5, not built yet
make eval                 # Phase 6, not built yet

Unbuilt targets exit 1 with the phase they land in; that's expected, not a
broken setup. Everything runs through `uv run` — don't call bare `python`
(system python is 3.9).