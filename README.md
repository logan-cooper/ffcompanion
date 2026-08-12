# ffcompanion

AI powered Fantasy Football companion for start/sit, waiver wire, and trade advice.

The user chats freely; the model calls deterministic tools that read a local
stats warehouse, so every number in an answer traces back to real data. See
[docs/ROADMAP.md](docs/ROADMAP.md) for the full build plan.

## Status

Phase 5 complete — stats warehouse (2023–2025), Sleeper league ingestion with
format detection, a league-aware scoring engine, format-aware valuation,
year-round validity, the six-tool layer, and a working chat agent.

## Runs entirely on your machine, for free

There is **no API key in this project and no per-token cost**. The model runs
locally through [Ollama](https://ollama.com), so a question costs $0 whether one
person uses this or your whole league does.

That works because the model supplies no knowledge — only judgment. Every
statistic comes from a tool that reads the local warehouse, and the system
prompt forbids stating a number that didn't. The model's job is picking tools,
formatting arguments, and writing prose over the results, which a 7–8B
open-weights model does well. **Nothing here is trained or fine-tuned**; that
would teach the model football facts it is explicitly not allowed to use.

```sh
brew install ollama          # or see ollama.com for Linux/Windows
ollama serve                 # leave running
ollama pull qwen3:8b         # ~5GB, once
make chat
```

**Hardware:** ~16GB RAM is a realistic floor. A 7–8B model at 4-bit
quantization is ~5GB on disk and ~6GB resident alongside the warehouse. On 8GB
it will be painful.

**Speed is the tradeoff.** A turn takes tens of seconds rather than one or two,
and the eval suite runs 15–20 minutes. That is the price of free, and it is
worth it here.

### Choosing a model

Running locally, model choice is the largest variable in the system and it
cannot be settled by reputation. `make eval-compare` runs the same suite against
several candidates and ranks them on the two things that decide it — whether the
model calls the right tools, and whether it invents numbers:

```sh
make eval-compare MODELS=qwen3:8b,llama3.1:8b,hermes3:8b
```

```
model                  passed  tools ok  grounded  fabricated    min
------------------------------------------------------------------
qwen3:8b                  …/12      …/12       …%           …      …
```

Speed is reported but is only a tiebreak: a fast model that fabricates
statistics is useless to an app whose entire premise is traceable numbers.

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
| `make tools-demo` | Print all six tools under both a dynasty and a redraft context |
| `make chat` | Conversational REPL, running locally at no cost |
| `make eval MODEL=qwen3:8b` | Eval suite — how a model gets chosen |
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

## Valuation

`get_valuation(ctx)` is the only entry point, and the only place format is
branched on. Both strategies return the same shape: `PlayerValue` always carries
**both** `win_now` and `future`, so the two numbers survive all the way to the
response and the model can name the tradeoff instead of collapsing it.

In redraft, `future` is 0 and picks are worth nothing — not ignored, just
correctly zero. Dynasty adds discounted production over a 3-year horizon, bent
by positional aging curves, plus picks as real assets.

Same player, same data, different worth (from the two linked dynasty leagues,
one marked `contend` and one `rebuild`):

| Player | Age | win_now | future | Contender | Rebuilder |
|---|---|---|---|---|---|
| Christian McCaffrey | 29.2 | 58.8 | 0.0 | **47.1** | 11.8 |
| Marvin Harrison Jr. | 23.1 | 14.7 | 108.2 | 33.4 | **89.5** |
| Jonathan Taylor | 26.6 | 20.8 | 16.8 | 20.0 | 17.6 |

The gate test trades the first for the second and asserts the verdict flips
three ways: a loss in redraft (−44), a gain under dynasty+rebuild (+78), and a
loss again under dynasty+contend (−14).

Design notes worth knowing:

- **Values are floored at zero.** Replacement level is what the waiver wire
  gives away, so a player below it is worth nothing rather than being a
  liability. Before flooring, a 31-year-old back scored −279 future and the
  model would have recommended paying someone to take him.
- **Intent is ignored in single-year formats.** Not merely because `future` is
  0 there — applying the split anyway rescales `win_now` and makes the same
  redraft player look four times better to a contender than a rebuilder.
- **Aging curves are data, not logic** ([valuation/aging.py](src/advisor/valuation/aging.py)).
  RBs fall off a cliff at 27–28, WRs plateau into their late twenties, TEs peak
  late, QBs barely decline before 37.
- **Discount rate (0.75/yr) is the main dial** between contend-leaning and
  rebuild-leaning advice, deliberately a named constant.

**Known limitation:** `future` ages a player's *current* production, so a young
player already below replacement gets zero future value. That under-rates
unproven breakout candidates, which matters most to a rebuilding team. Fixing it
properly means modelling development upside — worth revisiting only once the
evals in Phase 6 show it actually costs answers.

## Year-round validity

The app has to work in February and in week 1, not only in November. Both are
handled by one shrinkage formula rather than season-phase branching:

```
blended = (games_this_year x current_signal + 6 x last_season_baseline)
          / (games_this_year + 6)
```

| Week | Weight on last season | McCaffrey projection |
|---|---|---|
| 0 (offseason) | 100% | 11.95 |
| 1 | 86% | 13.56 |
| 3 | 67% | 15.73 |
| 6 | 50% | 18.72 |
| 17 | 27% | 21.73 |

Without it, week 1 projected a full season off a single game — 23.2 pts/gm and
a `win_now` of 201.6 from one outing.

**Offseason dynasty** works because player identity resolves from the most
recent season with data, with age advanced to the season being valued, and
`games_remaining` returns a full slate before kickoff instead of zero.
Previously a February valuation returned `position=None, age=None, value=0` for
every player — dead for more than half the year, in the format that trades
year-round.

Two subtleties worth knowing:

- **The prior baseline is a flat season mean**, deliberately not the
  recency-weighted blend used within a season. Recency predicts next week
  because it catches role changes; it does not predict next year, and the last
  three games are the worst possible sample — week 18 is when playoff teams sit
  starters. Weighting them priced a 24-year-old receiver at 7.3/gm instead of
  10.7 purely because he missed the finale.
- **Aging is applied relative to now, not to peak.** Today's production already
  reflects today's age, so the year-over-year factor is the *ratio* between two
  points on the curve. Using the absolute value double-counts the decline and
  turned a 29-year-old back's realistic 24% drop into 56%.

## Tools

Six pure functions the model calls. Each returns a JSON-serialisable dict and
caps its own output.

**They take no session identifiers.** `league_id` and the user's own `roster_id`
are bound by the agent loop, not asked of the model — it cannot know those
values, and a real trace showed it passing the league's *name* where an id
belonged. Every argument removed from a schema is one fewer way a small model
produces an unusable call.

| Tool | Purpose |
|---|---|
| `resolve_player` | Name → candidate player_ids. Called first for any named player |
| `get_my_roster` | One team in depth: slots, form, age, win-now/future, picks |
| `get_league_rosters` | All teams, compact: positional strength and roster age |
| `compare_players` | ≤4 players: weekly points, usage, upcoming matchup, values |
| `get_available_players` | Free agents ranked on recent form, with trending adds |
| `evaluate_trade` | Point deltas for both sides. Players and picks. **No verdict** |

**Tools never branch on format.** They ask `get_valuation(ctx)` and report what
comes back, which is what keeps signatures identical across redraft and dynasty.
`make tools-demo` proves it by running all six with the *same arguments* under
both, e.g. the same trade:

| | `win_now_delta` | `future_delta` | intent-weighted |
|---|---|---|---|
| dynasty | −37.8 | **+42.95** | **+26.8** (a gain) |
| redraft | −37.8 | 0.0 | **−37.8** (a loss) |

Every response carries the same envelope: league format, team intent, where in
the season we are, which season the stats came from, and when the data was
fetched. Echoing that back on every call is cheaper and more reliable than
hoping the model remembers the system prompt twelve turns later.

Two details that exist to stop the model misreading a number:

- **A completed season labels its own zeroes.** With no games left, `win_now` is
  0 for everyone by arithmetic. Unlabelled, `win_now: 0.0` beside an elite
  receiver reads as a verdict, so the envelope says so explicitly.
- **Truncation follows priority, not length.** Responses are capped near 1500
  tokens; a 30-player dynasty roster overruns it. Trimming whichever list is
  longest would drop starters, so the least decision-relevant lists (IR, taxi,
  picks, bench) are emptied first and starters go last.

**Careful:** `team_intent` is user-set and survives re-linking, but not deleting
`data/advisor.duckdb`. A full warehouse rebuild loses it; re-run `set-intent`.

**No fantasy points are stored anywhere.** Points depend on a league's scoring
settings and are computed at query time in Phase 3. nflverse publishes
`fantasy_points` columns and they are dropped on purpose; a test asserts no such
column exists.

## Layout

```
pyproject.toml
.env.example          # MODEL, OLLAMA_HOST, DB_PATH, DEFAULT_LEAGUE_ID
Makefile
data/                 # gitignored — DuckDB file + Parquet cache
src/advisor/
  config.py           # env loading, typed settings object
  db.py               # repository layer: get_conn() + query()
  sources/            # nflverse.py (Phase 1), sleeper.py (Phase 2)
  warehouse/          # schema.py, ingest.py, leagues.py (Phases 1-2)
  scoring/            # league rules -> points, format-agnostic (Phase 3)
  valuation/          # what a player/pick is worth, format-aware (Phase 3b)
  tools/              # the six functions the model calls (Phase 4)
  agent/              # backend seam, Ollama client, loop, prompt (Phase 5)
  evals/              # harness that picks the model (Phase 6)
  cli.py
evals/cases.yaml
tests/
```

**The backend is one seam.** `agent/backend.py` defines the interface;
`agent/ollama.py` is the only module that speaks HTTP. The loop, the tools, and
everything below them never learn what is generating tokens — so swapping
runtimes later touches one file.

**Database access rule:** nothing outside [src/advisor/db.py](src/advisor/db.py)
imports `duckdb`. Every other module uses `get_conn()` and `query()`. That is
what keeps the Phase 8 swap to Postgres a one-file change.
