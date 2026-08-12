# ffcompanion

AI powered Fantasy Football companion for start/sit, waiver wire, and trade advice.

The user chats freely; the model calls deterministic tools that read a local
stats warehouse, so every number in an answer traces back to real data. See
[docs/ROADMAP.md](docs/ROADMAP.md) for the full build plan.

## Status

Phase 3 complete — stats warehouse (2023–2025), Sleeper league ingestion with
format detection, and a league-aware scoring engine. No LLM calls yet.

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
| `make warehouse` | Build the full warehouse (2023–2025) in one command |
| `make ingest SEASON=2025` | Load a single season |
| `make ingest SEASONS=2023,2024,2025` | Load several |
| `make status` | Show what has been ingested and when |
| `make link-league USERNAME=you SEASON=2025` | Pull your Sleeper leagues |
| `make verify-scoring` | Check scoring against points Sleeper actually recorded |
| `make chat` | Conversational REPL (Phase 5) |
| `make eval` | Eval suite (Phase 6) |
| `make clean` | Remove the local database, caches, and build artifacts |

Ingest is idempotent — re-running replaces rows rather than duplicating them.
`--week N` (repeatable) rewrites a single week; `--refresh` re-downloads instead
of reusing the Parquet cache. After the first run everything is served from
`data/cache/` (~1.6MB), so a full three-season rebuild takes under a second and
needs no network.

**Why three seasons.** A single season badly misreads anyone who lost time to
injury. Malik Nabers in 2025: 271 yards in 4 games — on that data alone he looks
finished, when in fact he is a 22-year-old who put up 1,206 yards as a rookie the
year before. Three years also matches the multi-year window dynasty valuation
needs in Phase 3b. Going back further mostly adds noise, since team and scheme
changes make older seasons a poor guide.

**Prior seasons are context, not recent form.** Season-scoped data must stay
labeled by season and never be averaged into one blended number — "averages 700
yards a season" describes nobody. The views enforce the boundary: both
`v_player_season_totals` and `v_player_rolling_3wk` partition by season, so a
week-1 rolling average never reaches back into the previous year.

## Warehouse

| Table | Grain |
|---|---|
| `players` | player × season — identity, the nflverse↔Sleeper id crosswalk, and the age/draft fields dynasty valuation needs |
| `player_week_stats` | player × week — raw counting stats only |
| `player_week_usage` | player × week — snap/target/air-yards share and red-zone volume |
| `schedules` | game |
| `ingest_log` | source × season × week — answers "is my data stale?" |

League tables (Phase 2): `leagues` (raw `scoring_settings` stored verbatim),
`league_users`, `league_rosters`, `traded_picks`, `available_players`,
`team_intent`.

Views: `v_player_season_totals`, `v_player_rolling_3wk` (trailing 3 games),
`v_position_defense_rank` (rank 1 = fewest yards allowed to that position).

## League format

Format is read from the league, never guessed. The Sleeper `settings.type`
mapping was verified against real leagues rather than taken from documentation —
worth doing, because the observed values did not match the common wisdom:

| `settings.type` | format | status |
|---|---|---|
| 2 | dynasty | confirmed against two live leagues |
| 3 | survival | confirmed against one live league |
| 0 | redraft | documented only — fixture-tested, no live league available |
| 1 | keeper | documented only — fixture-tested, no live league available |

`survival` is not in the roadmap's enum. It was found in real data and kept
distinct deliberately: a survival league has no persistent rosters, so answering
it with redraft logic would produce confident nonsense.

Detection never trusts `type` alone. Taxi squads and a `previous_league_id` are
carry-over features, so a league claiming `type=0` while having either resolves
to `unknown` — which the app must treat as "ask the user", never as a default.

`team_intent` (contend / rebuild / balanced) is **user-set, never inferred**, and
lives in its own table so re-linking a league cannot wipe it. Set it with
`advisor set-intent --league-id X --roster-id N --intent contend`.

## Scoring

`score_stat_line(raw_stats, scoring_settings, position=...)` reads the league's
own rules. Nothing about PPR, touchdown values, or bonuses is hard-coded — the
three linked leagues pay 4, 6, and 6 points per passing touchdown, and two score
first downs and 40+ yard plays that the third does not.

**Validated against reality, not against itself.** Sleeper publishes its own
computed points per player per week, so `make verify-scoring` scores every
warehouse stat line and diffs it:

```
814 survival            2065/2071  =  99.71%
Beer Ball Empire        4052/4137  =  97.95%
Wolfpack Dynasty        3809/3820  =  99.71%
TOTAL                   9926/10028 =  98.98% exact
```

Run it after touching [scoring/keys.py](src/advisor/scoring/keys.py) — a wrong
entry there yields plausible numbers that are quietly wrong, which no unit test
over hand-written stat lines will catch. It is how the touchdown/first-down rule
below was found.

Two things that check caught:

- **A touchdown is a first down to nflverse, but not to Sleeper.** Unadjusted,
  this over-scores every scoring play by one first down — 744 wrong player-weeks
  in one league. Fixing it moved that league from 66.5% to 97.9%.
- **`special_teams_tds` was missing entirely** (found in Phase 1 by the same
  method against nflverse's own PPR figure): 27 return touchdowns, each a silent
  6-point error.

The residual ~1% is a stat-source difference, not a mapping bug: Sleeper's feed
records fumbles on plays nflverse does not attribute to the player. `pass_int_td`
(a pick-six charged to the quarterback) is not derivable from weekly stats and is
reported as unsupported rather than silently skipped.

Projections are **deliberately dumb** — 60% trailing-3-game average, 40% season
average, opponent adjustment capped at ±15%. No model. Every knob is a named
constant, because a heuristic that can be explained in one sentence beats a
better-fitting one that cannot.

`positional_scarcity()` derives replacement level from the league's own
`roster_positions` × team count. Superflex is visible in the output: QB
replacement runs ~16–18 pts/gm against ~10–12 for RB/WR/TE, which is why a
superflex slot moves QB value more than any other setting.

**No fantasy points are stored anywhere.** Points depend on a league's scoring
settings and are computed at query time in Phase 3. nflverse publishes
`fantasy_points` columns and they are dropped on purpose; a test asserts no such
column exists.

## Layout

```
pyproject.toml
.env.example          # ANTHROPIC_API_KEY, DB_PATH, DEFAULT_LEAGUE_ID
Makefile
data/                 # gitignored — DuckDB file + Parquet cache
src/advisor/
  config.py           # env loading, typed settings object
  db.py               # repository layer: get_conn() + query()
  sources/            # nflverse.py (Phase 1), sleeper.py (Phase 2)
  warehouse/          # schema.py + ingest.py (Phase 1)
  scoring/            # league rules -> points, format-agnostic (Phase 3)
  valuation/          # what a player/pick is worth, format-aware (Phase 3b)
  tools/              # the six functions the model calls (Phase 4)
  agent/              # tool-use loop + system prompt (Phase 5)
  cli.py
tests/
```

**Database access rule:** nothing outside [src/advisor/db.py](src/advisor/db.py)
imports `duckdb`. Every other module uses `get_conn()` and `query()`. That is
what keeps the Phase 8 swap to Postgres a one-file change.
