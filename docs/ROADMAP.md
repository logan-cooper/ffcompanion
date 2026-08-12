# Fantasy Football Advisor — Implementation Roadmap

**Local-first build. No cloud infrastructure until Phase 8.**

A conversational AI companion for in-season fantasy football decisions (trades,
waivers, start/sit). The user chats freely; the model calls deterministic tools
that read a local stats warehouse, so every number in an answer traces back to
real data.

**Supports both redraft and dynasty leagues.** This is not a cosmetic flag — the
two formats disagree about what a good decision *is*, so league format is a
first-class concept threaded through Phases 1–6. See "League format" below.

---

## How to use this document

Each phase below is self-contained and written so it can be picked up cold. To
start a phase, paste its heading and body into a new conversation with Claude
along with this line:

> "We're building the fantasy football advisor described in this roadmap.
> Implement Phase N. Here's my current repo state: [paste tree / relevant files]."

Do not skip ahead. Each phase has a **Done when** test that must pass before the
next phase makes sense. Phases 1–4 involve **zero LLM API calls** — that is
intentional. Most of the app's correctness lives in code that has nothing to do
with the model, and debugging it through a chat interface is miserable.

---

## Architecture summary (read this before any phase)

```
┌─────────────────────────────────────────────────────────┐
│  CLI chat (Phase 5)  →  HTTP + web UI (Phase 7)         │
└────────────────────────┬────────────────────────────────┘
                         │
              ┌──────────▼──────────┐
              │  Agent loop         │  Phase 5
              │  (Anthropic API)    │
              └──────────┬──────────┘
                         │ tool_use / tool_result
              ┌──────────▼──────────┐
              │  Tool registry      │  Phase 4
              │  (6 pure functions) │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │  LeagueContext      │  Phase 4
              │  (format + intent)  │
              └──────────┬──────────┘
                         │
         ┌───────────────┼───────────────┬────────────────┐
         │               │               │                │
┌────────▼──────┐ ┌──────▼───────┐ ┌────▼──────────┐ ┌───▼──────────┐
│ Scoring engine│ │  Valuation   │ │ League store  │ │Stats warehouse│
│  (format-     │ │  Redraft |   │ │   Phase 2     │ │   Phase 1     │
│   agnostic)   │ │  Dynasty     │ │               │ │               │
│   Phase 3     │ │   Phase 3    │ │               │ │               │
└───────────────┘ └──────────────┘ └───────────────┘ └───────────────┘
                         │
                    DuckDB file (local)  →  Postgres (Phase 8)
```

**Language:** Python 3.12. Single service. The NFL data ecosystem
(`nflreadpy`, `nfl_data_py`) is Python-native and not worth fighting.

**Local database:** DuckDB. Single file, no daemon, reads nflverse Parquet
natively, and excellent at the rolling-window analytical queries this app needs.
All database access goes through a thin repository layer so Phase 8 can swap in
Postgres without touching tool code.

**External data sources:**
- **Sleeper API** — `https://api.sleeper.app/v1/...`. Free, read-only, no auth
  token. Leagues, rosters, users, matchups, transactions, drafts, trending
  players. Stay under ~1000 requests/minute.
- **nflverse** — weekly player stats, play-by-play, schedules, snap counts,
  rosters. CC-BY 4.0, refreshed weekly during the season. Accessed via
  `nflreadpy` (Polars-based) or direct Parquet URLs from `nflverse-data`
  GitHub releases.

**Season context:** The 2026 NFL regular season runs Sept 9, 2026 – Jan 10,
2027. nflverse refreshes weekly *after* games, not live in-game — this app
answers "what does the season so far tell me" questions, not live-scoring
questions.

### League format

Two things determine what counts as good advice, and both must be known before
any recommendation is generated:

**1. Format** — read from the league, never guessed.
- *Redraft:* rosters reset each year. Only rest-of-season value matters. A
  27-year-old RB and a 22-year-old RB producing identically are worth the same.
- *Dynasty / keeper:* rosters carry over. Player age, contract-like control, and
  tradeable future draft picks all carry value. A trade can lose points this
  season and still be clearly correct.

**2. Team intent** — set by the user, not inferred.
`contend` | `rebuild` | `balanced`. In dynasty the *same* trade is good for a
rebuilding team and bad for a contender. Record and roster age hint at this but
guess wrong often enough to produce confidently bad advice. Ask, don't infer.

**Design rule:** tools do not branch on format. Format and intent live in a
`LeagueContext` object resolved once per request; the valuation strategy switches
behind a single interface. Tool signatures are identical in both formats. What
changes is what the numbers *mean*, and dynasty responses carry both a win-now
and a future-value figure so the model can articulate the tradeoff instead of
collapsing it into one score.

---

## Phase 0 — Project skeleton

**Goal:** A runnable, dependency-managed project with nothing in it.

**Build:**
- `uv` for dependency management (`uv init`, `uv add ...`)
- Repo layout:
  ```
  fantasy-advisor/
    pyproject.toml
    .env.example          # ANTHROPIC_API_KEY, DB_PATH, DEFAULT_LEAGUE_ID
    README.md
    Makefile              # make ingest / make chat / make test / make eval
    data/                 # gitignored — DuckDB file + Parquet cache
    src/advisor/
      __init__.py
      config.py           # env loading, typed settings object
      db.py               # repository layer: connection + query helpers
      sources/            # sleeper.py, nflverse.py
      scoring/            # league rules → points (format-agnostic)
      valuation/          # what a player/pick is worth (format-aware)
      tools/
      agent/
      cli.py
    tests/
  ```
- `config.py` loads from `.env` via `pydantic-settings`. No secrets in code.
- `db.py` exposes `get_conn()` and `query(sql, params) -> list[dict]`. Every
  later phase uses only these two functions — no direct DuckDB imports anywhere
  else. This is what makes Phase 8's Postgres swap a one-file change.

**Done when:** `make test` runs an empty pytest suite green, and
`python -m advisor.cli --version` prints a version.

---

## Phase 1 — Stats warehouse

**Goal:** A local database holding current-season player production, refreshable
with one command.

**Build:**
- `sources/nflverse.py`: fetch weekly player stats and schedules for a given
  season. Cache raw Parquet under `data/cache/` so re-runs are offline and fast.
- Schema (DuckDB tables):
  - `players` — player_id, name, position, team, plus nflverse↔Sleeper ID
    crosswalk (nflverse publishes an ID-mapping table; use it, do not fuzzy-match
    names). **Also: birth_date, age, years_exp, rookie_year, draft_round,
    draft_pick.** nflverse rosters carry all of these. Dynasty valuation is
    impossible without age, and retrofitting it later means re-ingesting
    everything — capture it now even though redraft ignores it.
  - `player_week_stats` — one row per player per week, **raw counting stats
    only**: completions, attempts, passing_yards, passing_tds, interceptions,
    carries, rushing_yards, rushing_tds, receptions, targets, receiving_yards,
    receiving_tds, fumbles_lost, two_point_conversions. Do **not** store a
    fantasy_points column (see Phase 3).
  - `player_week_usage` — snap_share, target_share, air_yards_share,
    red_zone_touches.
  - `schedules` — season, week, home_team, away_team, so "upcoming opponent" is
    answerable.
  - `ingest_log` — source, season, week, row_count, fetched_at. You will need
    this to answer "is my data stale?"
- `make ingest SEASON=2025` populates everything. Idempotent: re-running
  replaces a week rather than duplicating it.
- Derived views (SQL, not Python): `v_player_rolling_3wk`,
  `v_player_season_totals`, `v_position_defense_rank`.

**Why 2025 first:** Build and verify against a completed season with known
answers. You cannot debug a stats pipeline against a season that hasn't started.

**Done when:** `make ingest SEASON=2025` completes, and a test asserts that a
handful of hand-verified 2025 stat lines (pick three players you can look up
manually) match the database exactly.

---

## Phase 2 — Sleeper league ingestion

**Goal:** Pull a real league's structure into the local database.

**Build:**
- `sources/sleeper.py` — a thin typed client. Endpoints needed:
  - `GET /user/{username}` → user_id
  - `GET /user/{user_id}/leagues/nfl/{season}` → league list
  - `GET /league/{league_id}` → settings, **scoring_settings**, roster_positions
  - `GET /league/{league_id}/rosters` → roster_id, owner_id, players[], starters[]
  - `GET /league/{league_id}/users` → display names
  - `GET /league/{league_id}/matchups/{week}`
  - `GET /league/{league_id}/transactions/{week}`
  - `GET /players/nfl` → full player dump (large; cache to disk, refresh weekly)
  - `GET /players/nfl/trending/add?lookback_hours=24` → waiver signal
  - `GET /league/{league_id}/traded_picks` → **dynasty-critical.** Future draft
    picks are tradeable assets. Without this you cannot evaluate a large share of
    real dynasty trade proposals.
- Tables: `leagues` (including the raw `scoring_settings` JSON — store it
  verbatim), `league_rosters`, `league_users`, `traded_picks`.

**League format detection.** Derive and store three fields on `leagues`:
- `format` — one of `redraft` | `keeper` | `dynasty`. Sleeper's league object
  carries a `settings.type` field for this, and `previous_league_id` is non-null
  for any continuing league. **Verify the exact enum values against your own
  leagues** rather than trusting any documented mapping — it takes two minutes
  and everything downstream depends on it. If detection is ambiguous, store
  `unknown` and make the app ask the user rather than defaulting silently.
- `superflex` — true if `roster_positions` contains `SUPER_FLEX`. Common in
  dynasty and it changes QB valuation more than any other single setting.
- `team_intent` — `contend` | `rebuild` | `balanced`, defaulting to `balanced`.
  User-set, not derived. Stored per league per user.
- Compute and store `available_players`: everyone in the player pool not on any
  roster in the league. This is your waiver-wire universe and it must be derived
  per league, not globally.
- Retry with backoff; treat Sleeper as flaky.

**Done when:** `make link-league USERNAME=<you> SEASON=2025` (use a real league
you were in) prints your roster, each opponent's roster, and the count of
available free agents — plus the detected format, superflex flag, and any traded
picks. If you have access to both a redraft and a dynasty league, run it against
both and confirm the format field differs. If you only have one, hand-write a
fixture for the other; do not proceed to Phase 3 with an untested detector.

---

## Phase 3 — Scoring engine

**Goal:** Convert raw stat lines into fantasy points *using a specific league's
rules*. This is the highest-value component in the app and the one most similar
projects get wrong.

**Build:**
- `scoring/engine.py`: `score_stat_line(raw_stats: dict, scoring_settings: dict)
  -> float`. Sleeper's `scoring_settings` is a flat map of stat key → point value
  (`rec: 0.5`, `pass_td: 4`, `rush_yd: 0.1`, `fum_lost: -2`, ...). Multiply and
  sum. Handle bonus keys (`bonus_rec_te`, `bonus_pass_yd_300`) and missing keys
  (absent = 0 points).
- A mapping table from nflverse column names to Sleeper stat keys. Keep it in
  one dict, documented, because it will be the source of subtle bugs.
- `scoring/projections.py`: rest-of-season projection. **Start deliberately
  dumb** — a weighted average of the last 3 games and season average, adjusted by
  opponent defensive rank. Do not build a machine learning model. The value of
  this app is grounded reasoning over real usage data, not projection accuracy,
  and a transparent heuristic you can explain beats a black box you can't.
- A `positional_scarcity(league_id, position)` helper: replacement-level points
  at each position given this league's roster requirements (and superflex, which
  materially raises QB replacement level). Trade evaluation is meaningless
  without it.

**Keep `scoring/` format-agnostic.** It answers one question — "how many points
did this stat line produce under these rules" — and that answer is identical in
redraft and dynasty. Do not put age or format logic in here.

**Done when:** a test scores a full week of 2025 stat lines under both a
half-PPR and a full-PPR settings object and the two differ by exactly
0.5 × receptions for every player. Then spot-check three players against the
actual points Sleeper recorded in a real league.

---

## Phase 3b — Valuation layer (format-aware)

**Goal:** Turn points into *worth*, differently for each format, behind one
interface. This is the phase that makes the app useful in dynasty rather than
just tolerant of it.

**Build** — `valuation/base.py` defines the interface both strategies implement:

```python
class Valuation(Protocol):
    def player_value(self, player_id: str, ctx: LeagueContext) -> PlayerValue
    def pick_value(self, season: int, round_: int, ctx: LeagueContext) -> float
    def roster_value(self, roster_id: str, ctx: LeagueContext) -> RosterValue
```

`PlayerValue` carries **both** figures in every format: `win_now` (rest-of-season
points above replacement) and `future` (multi-year value). In redraft, `future`
is simply zero — that keeps one code path instead of two and lets the tools stay
identical.

- `valuation/redraft.py` — `win_now` = rest-of-season projection above
  positional replacement level. `future` = 0. `pick_value` = 0 (picks aren't
  assets in redraft).
- `valuation/dynasty.py` — the real work:
  - **Positional aging curves.** RBs decline sharply from around 27; WRs hold
    through their late twenties and fall off later; TEs peak late and hold; QBs
    hold longest by a wide margin. Implement as a per-position multiplier
    function of age, in one documented module, with the curve shape as data you
    can tune rather than logic you have to rewrite.
  - **Multi-year horizon.** Sum discounted projected value over roughly a 3-year
    window. Make the discount rate a config value — it's the main dial between
    contend-leaning and rebuild-leaning advice.
  - **Draft pick values.** Start with a published consensus pick-value chart as
    static data (early/mid/late 1st, 2nd, 3rd, by season distance). Do not try to
    derive your own from scratch; it's a research project, not a feature.
  - **Superflex adjustment.** A multiplier on QB value when
    `ctx.superflex` is true.
- `valuation/intent.py` — applies `team_intent` as a weighting between `win_now`
  and `future` when producing a single comparable number. `contend` weights
  win-now heavily, `rebuild` inverts it, `balanced` splits. Keep this as the
  *last* step so the underlying two numbers are always available unweighted.
- `valuation/__init__.py` — `get_valuation(ctx) -> Valuation`, the only entry
  point tools use. One factory function; no format checks anywhere else.

**Done when:** a test asserts that a specific trade (an aging productive RB for a
younger lower-producing WR) evaluates as a *loss* under `RedraftValuation` and a
*gain* under `DynastyValuation` with `intent=rebuild` — and that the dynasty
verdict flips to a loss under `intent=contend`. If that test passes, the format
abstraction is real and not decorative.

---

## Phase 3c — Year-round validity

**Goal:** Useful in February and in week 1, not only in November.

Two calendar assumptions are baked into a naive build of Phases 1–3b, and both
make the app confidently wrong at the times people most want to use it.

**The offseason problem (dynasty).** A dynasty league rolls to the next season in
January and trades hardest between February and the rookie draft. But `players`
is keyed `(player_id, season)`, so before the new season has any data a lookup
finds nothing: every player resolves to `position=None, age=None` and prices at
zero. The app is dead for more than half the year, in the format that most needs
year-round answers.

**The early-season problem (redraft).** In week 1 there is one game of evidence.
Projecting seventeen games from it turns a fluke opener into an elite season and
a quiet one into a bust. Meanwhile the prior seasons already sitting in the
warehouse go unused.

**Build:**
- `players.py`: resolve identity from the most recent season at or before the
  one being valued, and report age **as of the valuation season**. Reusing last
  season's ages hands every roster a free year of youth — backwards for dynasty.
- Shrinkage in `scoring/projections.py`. One formula, no season-phase branching:

  ```
  blended = (games_this_year x current_signal + K x prior_baseline)
            / (games_this_year + K)
  ```

  At `n=0` it is purely last season; by week 17 last season contributes about a
  quarter. `K = 6` puts the crossover near week 6.
- The prior baseline is a **flat season mean**, deliberately not the
  recency-weighted blend used within a season. Recency predicts next week
  because it catches role changes; it does not predict next year, and the last
  three games are the worst possible sample — week 18 is when playoff teams rest
  starters.
- `LeagueContext` gains `stats_season`, `current_week` inferred from the data
  rather than assumed complete, and `games_remaining` that returns a full slate
  before kickoff instead of zero.
- Aging must be applied **relative to now**, not as share-of-peak. Today's
  production already reflects today's age, so the year-over-year factor is the
  ratio between two points on the curve. Using the absolute value double-counts
  the decline — a 29-year-old back's 24% drop becomes 56%.

**Done when:** an offseason dynasty context (next season, zero games played)
returns non-zero, age-aware values for real players; a week-1 projection sits
between that week's game and last season's baseline rather than extrapolating
the single game; and late-season answers are unchanged.

---

## Phase 4 — Tool layer (no LLM yet)

**Goal:** Six pure functions, fully tested, that the model will later call.

**First, build `LeagueContext`** (`tools/context.py`):
`load_context(league_id, roster_id) -> LeagueContext`, carrying scoring_settings,
roster_positions, format, superflex, team_intent, and current week. Resolved once
per request and passed down. **This is the only place format is read.** No tool
body, and no schema field, mentions redraft or dynasty — if a tool needs an
`if format == "dynasty"` branch, the valuation interface is wrong and should be
fixed there instead.

**Then the six tools** — each a plain Python function returning a
JSON-serializable dict. Every one takes `league_id`. Every one caps its output
size.

1. `resolve_player(query, league_id)` → candidate list with player_id, name,
   position, team, and current roster owner. **Always called first** for any
   named player; name→ID ambiguity is the app's #1 failure mode.
2. `get_my_roster(league_id, roster_id)` → starters, bench, with season points,
   last-3-week trend, and (dynasty) age plus win-now/future values per player.
   Also returns owned draft picks when the format has them.
3. `get_league_rosters(league_id)` → all teams, compact; positional strength
   summary rather than full stat lines. In dynasty, include each roster's average
   age at RB/WR — that's how the model spots who's rebuilding and who's pushing.
4. `compare_players(league_id, player_ids[≤4], weeks≤8)` → per-week points,
   rolling averages, snap/target share, upcoming opponent rank, and both value
   figures from the valuation layer.
5. `get_available_players(league_id, position, limit≤15)` → free agents ranked by
   recent production, with trending-add counts from Sleeper.
6. `evaluate_trade(league_id, my_roster_id, their_roster_id, i_give[], i_get[])`
   → **`i_give` and `i_get` accept both player_ids and pick references** (e.g.
   `"2027-1st"`), so dynasty proposals involving picks work without a second
   tool. Returns win-now delta *and* future delta for both sides,
   scarcity-adjusted, plus post-trade starter/bench depth.
   **Returns numbers only — no verdict.**

**Rules for all tools:**
- Return data, never prose or opinions.
- Include a `data_as_of` field (from `ingest_log`) in every response.
- Include `format` and `team_intent` in every response. The model needs to know
  which frame it's reasoning in, and echoing it back is cheaper and more reliable
  than hoping it remembers from the system prompt.
- On no data, return `{"error": "...", "detail": "..."}` — never an empty
  success.
- Keep responses under ~1500 tokens; truncate and say so.

**Also build** `tools/registry.py`: `TOOLS` (the JSON schema list sent to the
API) and `REGISTRY` (name → callable). Schemas are written by hand and the
`description` field is treated as prompt engineering — spend real time there.

**Done when:** every tool has tests calling it directly with your 2025 league,
and `make tools-demo` prints the output of all six **under both a redraft and a
dynasty context**, using the same tool arguments. You should be able to answer a
trade question yourself by reading raw tool output. If you can't, the model won't
be able to either.

---

## Phase 5 — Agent loop

**Goal:** Open-ended conversation in the terminal.

**Build:**
- `agent/loop.py`: the standard tool-use loop — call the API, if
  `stop_reason == "tool_use"` execute every `tool_use` block, append all results
  as a single user message, repeat. Hard cap of 8 iterations. Catch tool
  exceptions and return them *as tool results* so the model can self-correct
  (usually by re-resolving a name) instead of crashing the request.
- `agent/prompt.py`: the system prompt, **assembled per league** rather than a
  static string. Must include:
  - The current league's name, scoring rules, roster positions, and superflex
    status.
  - Today's date and the current NFL week.
  - **A format-specific reasoning block.** For redraft: "This is a redraft
    league. Rosters reset after this season. Ignore player age and future value
    entirely — only rest-of-season production matters." For dynasty: "This is a
    dynasty league; the user's stated intent is `{intent}`. Weigh win-now against
    future value accordingly, and always name which one you're prioritizing and
    why. Player age and draft picks are real assets."
  - "Never state a statistic that did not come from a tool result. If a tool
    returns no data, say so plainly. Do not estimate."
  - "Call `resolve_player` before any tool that takes a player_id."
  - Instruction to state the tradeoff and give a recommendation, not a hedge —
    fantasy advice that refuses to commit is useless.
  - For unknown format: "Ask the user whether this is redraft or dynasty before
    giving trade advice." Never guess.
- `cli.py`: a REPL with conversation history, `/week`, `/league`, `/intent`
  (set contend/rebuild/balanced), `/format` (show or override detected format),
  `/reset`, and a `--verbose` flag that prints every tool call and result.
  That flag is your primary debugging tool for the rest of the project.
- Token accounting printed per turn so cost is visible from day one.

**Done when:** you can hold a multi-turn conversation — ask about a trade,
then follow up with "what if I add my TE?" — and `--verbose` shows sensible tool
calls with no invented numbers.

---

## Phase 6 — Eval harness

**Goal:** Know whether a prompt change made things better or worse.

**Build:**
- `evals/cases.yaml` — ~30 questions against your 2025 league with known-correct
  answers. Mix of: simple lookups, comparisons, trades, waiver questions,
  ambiguous player names, questions with no valid answer (a player who doesn't
  exist), and adversarial ones ("just tell me Bijan is a bust").
- **Every trade and roster-construction case runs twice — once per format** —
  with a `expects_different_answer: true` flag. The single highest-value eval in
  the suite is the paired age-for-production trade: if the redraft and dynasty
  answers come back the same, something upstream collapsed. Add dynasty-only
  cases too: a pick-for-player trade, "should I sell my 29-year-old RB," and
  "which of my young guys should I hold through a rebuild."
- Each case asserts on checkable properties, not exact wording: which tools were
  called, whether a specific number appears, whether a recommendation was given,
  whether it hallucinated a stat.
- `make eval` prints a pass/fail table and total token cost.

**Done when:** the suite runs in one command and you have a baseline score. From
here on, no prompt or tool change ships without re-running it. This is the part
of the project that actually teaches you agent engineering.

---

## Phase 7 — HTTP API and minimal web UI

**Goal:** Something with a URL, still running entirely on your laptop.

**Build:**
- FastAPI app: `POST /chat` (streaming via SSE), `POST /link-league`,
  `GET /health`, `GET /data-status`.
- Conversation persistence in DuckDB: `conversations`, `messages`. Load history
  on each request — the API is stateless, the database holds state.
- Single-page frontend: one HTML file, vanilla JS, streaming chat. No build step,
  no framework. Served by FastAPI as a static file. Resist scope creep here.
- `Dockerfile` + `docker-compose.yml` so the whole thing runs with
  `docker compose up` on any machine. This is the "downloadable project"
  milestone.

**Done when:** a fresh clone plus `docker compose up` gives a working chat app at
`localhost:8000` with only an Anthropic API key required.

---

## Phase 8 — Deployment

**Goal:** A public URL, with a hard spending cap.

**Build:**
- **Swap DuckDB → Postgres.** Only `db.py` and the DDL change. Rewrite the
  derived views in Postgres syntax; the tool layer should need no edits. If it
  does, the Phase 0 repository layer wasn't strict enough.
- **Railway Hobby** ($5/mo, which acts as a spending cap) with the opt-in hard
  spending limit configured *before* the first deploy. One service + Postgres,
  deployed from a git push. If you'd rather have a flat predictable invoice,
  Render's Starter web service ($7/mo) plus managed Postgres (from $6/mo) is the
  alternative.
- Weekly refresh job: cron (Tuesday morning, after Monday Night Football)
  running `make ingest SEASON=2026` and re-pulling league rosters.
- Rate limiting on `/chat` and a per-conversation token budget. Your real cost
  risk is Anthropic tokens, not hosting — an unauthenticated public chat endpoint
  is an open invoice.
- Basic auth or a signup flow before you share the link with your league.

**Done when:** your leaguemates can use it and your monthly bill is bounded by
configuration rather than by hope.

---

## Deliberately deferred

- **Draft-day mode.** Different data, different latency profile, different UX.
  Separate project.
- **ESPN and Yahoo league linking.** OAuth and undocumented endpoints. Sleeper
  only until the app is worth using.
- **Live in-game scoring.** nflverse refreshes weekly, not live. A different
  data source and a much harder problem.
- **`query_stats` escape hatch.** A tool taking a filter spec (position, weeks,
  sort field, limit) for "who leads the league in X" questions the six core
  tools miss. Add it in Phase 6 territory, once evals show which questions
  actually whiff. Do not let the model write raw SQL.
- **ML projections.** Only after the heuristic version is demonstrably the
  bottleneck.
- **Rookie draft support** (dynasty). Different from both in-season advice and
  redraft drafts — needs prospect data the stats warehouse doesn't have. Ships
  with draft-day mode or not at all.
- **Deriving your own pick-value chart.** Use published consensus values as
  static data. Revisit only if the app is otherwise finished.

---

## Suggested pacing (2026 season opens Sept 9)

| Week | Phases |
|---|---|
| 1 | 0, 1, 2 |
| 2 | 3, 3b |
| 3 | 4, 5 |
| 4 | 6, 7, then 8 |

Phase 3b is the one that will run long — the aging curves and pick values are
fiddly and worth getting right. If you're behind, ship redraft-only through
Phase 5 (`RedraftValuation` alone, dynasty leagues rejected with a clear message)
and add `DynastyValuation` during the season. The interface makes that a
genuinely additive change rather than a rewrite, which is the whole point of
splitting it out.

Phases 0–4 are the load-bearing work and involve no LLM calls. If you run short
on time, ship Phases 0–5 as a local CLI tool for yourself in week 1 of the
season and add the web layer during the bye weeks.
