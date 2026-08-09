# ffcompanion

AI powered Fantasy Football companion for start/sit, waiver wire, and trade advice.

The user chats freely; the model calls deterministic tools that read a local
stats warehouse, so every number in an answer traces back to real data. See
[docs/ROADMAP.md](docs/ROADMAP.md) for the full build plan.

## Status

Phase 0 complete — project skeleton. No data, no tools, no LLM calls yet.

## Setup

Requires [uv](https://docs.astral.sh/uv/). Python 3.12 is installed by uv.

```sh
brew install uv        # if you don't have it
make sync              # create .venv and install dependencies
cp .env.example .env   # then fill in values as later phases need them
make test
```

## Commands

| Command | What it does |
|---|---|
| `make sync` | Install/refresh the virtualenv from `uv.lock` |
| `make test` | Run the test suite |
| `make ingest SEASON=2025` | Load a season of nflverse stats (Phase 1) |
| `make chat` | Conversational REPL (Phase 5) |
| `make eval` | Eval suite (Phase 6) |
| `make clean` | Remove the local database, caches, and build artifacts |

## Layout

```
pyproject.toml
.env.example          # ANTHROPIC_API_KEY, DB_PATH, DEFAULT_LEAGUE_ID
Makefile
data/                 # gitignored — DuckDB file + Parquet cache
src/advisor/
  config.py           # env loading, typed settings object
  db.py               # repository layer: get_conn() + query()
  sources/            # sleeper.py, nflverse.py (Phases 1-2)
  scoring/            # league scoring + projections (Phase 3)
  tools/              # the six functions the model calls (Phase 4)
  agent/              # tool-use loop + system prompt (Phase 5)
  cli.py
tests/
```

**Database access rule:** nothing outside [src/advisor/db.py](src/advisor/db.py)
imports `duckdb`. Every other module uses `get_conn()` and `query()`. That is
what keeps the Phase 8 swap to Postgres a one-file change.
