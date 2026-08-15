# Fantasy Football Advisor — Implementation Roadmap

**Local-first, end to end. No cloud infrastructure, and no API key at any
phase.** The stats warehouse, the database, and the model all run on the user's
own machine, so a question costs $0 no matter how many people use the app.

A conversational AI companion for in-season fantasy football decisions (trades,
waivers, start/sit). The user chats freely; the model calls deterministic tools
that read a local stats warehouse, so every number in an answer traces back to
real data.

**The model supplies no knowledge — only judgment.** Every statistic comes from
a tool, and the model is forbidden from stating one that did not. That is what
makes a small open-weights model sufficient here, and it is why fine-tuning is
the wrong instinct: the model's job is choosing tools and writing prose over
their results, not knowing football. See Phase 5.

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
              │  local model via    │
              │  Ollama — no API    │
              │  key, $0 per query  │
              └──────────┬──────────┘
                         │ tool_use / tool_result
              ┌──────────▼──────────┐
              │  Tool registry      │  Phase 4
              │  (6 pure functions) │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │  LeagueContext      │  Phase 3c
              │  format + intent    │  (advisor/context.py)
              │  + season phase     │
              └──────────┬──────────┘
                         │
         ┌───────────────┼───────────────┬────────────────┐
         │               │               │                │
┌────────▼──────┐ ┌──────▼───────┐ ┌────▼──────────┐ ┌───▼──────────┐
│ Scoring engine│ │  Valuation   │ │ League store  │ │Stats warehouse│
│  (format-     │ │  Redraft |   │ │   Phase 2     │ │   Phase 1     │
│   agnostic)   │ │  Dynasty     │ │               │ │               │
│   Phase 3     │ │   Phase 3b   │ │               │ │               │
└───────────────┘ └──────────────┘ └───────────────┘ └───────────────┘
                         │
                    DuckDB file (local, permanently)
```

`LeagueContext` sits **below** the tool layer, not inside it: valuation needs the
type, and importing upward would invert the dependency. It lives in
`advisor/context.py` and carries the season phase (`current_week`,
`stats_season`, `is_offseason`) as well as format and intent — see Phase 3c.

**Language:** Python 3.12. Single service. The NFL data ecosystem
(`nflreadpy`, `nfl_data_py`) is Python-native and not worth fighting.

**Local database:** DuckDB. Single file, no daemon, reads nflverse Parquet
natively, and excellent at the rolling-window analytical queries this app needs.
All database access goes through a thin repository layer, which keeps storage
swappable and the tool layer ignorant of it. (The Postgres migration this was
originally for is gone — local-first makes DuckDB permanent — but the
discipline earns its keep anyway.)

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

**The calendar is not a detail.** Most of the year there is no "season so far":
dynasty leagues trade hardest between February and the rookie draft, and in
weeks 1–3 the current season is a handful of games. Every phase that touches
projections or valuation has to work at any point in the year — see Phase 3c,
which exists because building for mid-season is the default mistake.

### League format

Three things determine what counts as good advice, and all must be known before
any recommendation is generated:

**1. Format** — read from the league, never guessed.
- *Redraft:* rosters reset each year. Only rest-of-season value matters. A
  27-year-old RB and a 22-year-old RB producing identically are worth the same.
- *Dynasty / keeper:* rosters carry over. Player age, contract-like control, and
  tradeable future draft picks all carry value. A trade can lose points this
  season and still be clearly correct.
- *Other formats exist.* Survival leagues, for one, have no persistent rosters
  at all. Detect them and decline rather than answering them as redraft — see
  Phase 2. Anything unrecognised is `unknown`, which means **ask**.

**2. Team intent** — set by the user, not inferred.
`contend` | `rebuild` | `balanced`. In dynasty the *same* trade is good for a
rebuilding team and bad for a contender. Record and roster age hint at this but
guess wrong often enough to produce confidently bad advice. Ask, don't infer.
(A real 10-team league had two 3-11 teams, one with the second-youngest roster
and one with the second-oldest. Identical records, opposite correct advice.)

**3. Where in the year you are** — read from the data, never assumed.
Dynasty trades happen year-round and most happen in the offseason, when the
season being valued has zero games. In weeks 1–3 the current season is a
handful of games and last season is the better guide. A build that quietly
assumes mid-season is wrong most of the calendar — see Phase 3c.

**Design rule:** tools do not branch on format. Format, intent, and season phase
live in a `LeagueContext` object resolved once per request; the valuation
strategy switches behind a single interface. Tool signatures are identical in
both formats. What changes is what the numbers *mean*, and dynasty responses
carry both a win-now and a future-value figure so the model can articulate the
tradeoff instead of collapsing it into one score.

---

## Phase 0 — Project skeleton

**Goal:** A runnable, dependency-managed project with nothing in it.

**Build:**
- `uv` for dependency management (`uv init`, `uv add ...`)
- Repo layout:
  ```
  fantasy-advisor/
    pyproject.toml
    .env.example          # MODEL, OLLAMA_HOST, DB_PATH, DEFAULT_LEAGUE_ID
    README.md
    Makefile              # make ingest / make chat / make test / make eval
    data/                 # gitignored — DuckDB file + Parquet cache
    src/advisor/
      __init__.py
      config.py           # env loading, typed settings object
      db.py               # repository layer: connection + query helpers
      context.py          # LeagueContext (Phase 3c)
      players.py          # cross-season identity + age (Phase 3c)
      league_format.py    # format/superflex detection (Phase 2)
      sources/            # sleeper.py, nflverse.py
      warehouse/          # schema.py, ingest.py, leagues.py (Phases 1-2)
      scoring/            # league rules → points (format-agnostic)
      valuation/          # what a player/pick is worth (format-aware)
      tools/
      agent/
      cli.py
    tests/
  ```
  The exact tree matters less than the layering: `db.py` at the bottom,
  `context.py` below valuation, tools above everything.
- `config.py` loads from `.env` via `pydantic-settings`. No secrets in code.
- `db.py` exposes `get_conn()` and `query(sql, params) -> list[dict]`. Every
  later phase uses only these two functions — no direct DuckDB imports anywhere
  else. Originally this was to keep a Postgres swap cheap; that migration was
  dropped with the pivot to local-first, and the rule stays because one place
  that knows about storage is simply correct.

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
    only**. Do **not** store a fantasy_points column (see Phase 3). Store more
    than feels necessary: real leagues score things you would not guess, and a
    missing column is a silently wrong answer rather than an error. At minimum:
    completions, attempts, passing_yards, passing_tds, interceptions, carries,
    rushing_yards, rushing_tds, receptions, targets, receiving_yards,
    receiving_tds, fumbles, fumbles_lost, **the three 2pt conversion types
    separately** (Sleeper scores pass/rush/rec under distinct keys),
    special_teams_tds, first downs (passing/rushing/receiving), and counts of
    40+ yard plays.
  - `player_week_usage` — snap_share, target_share, air_yards_share, and
    red-zone volume. Red-zone usage is not published as a weekly stat; derive it
    from play-by-play, filtering to snaps inside the 20 *before* caching. Store
    carries and targets separately as well as their sum — "touches" is
    ambiguous and a later phase should never have to guess your definition.
  - `schedules` — season, week, home_team, away_team, so "upcoming opponent" is
    answerable.
  - `ingest_log` — source, season, week, row_count, fetched_at. You will need
    this to answer "is my data stale?"
- `make ingest SEASON=2025` populates everything. Idempotent: re-running
  replaces a week rather than duplicating it, scoped to season and (for weekly
  tables) to specific weeks.
- Derived views (SQL, not Python): `v_player_rolling_3wk`,
  `v_player_season_totals`, `v_position_defense_rank`. All three must partition
  by season so a week-1 rolling average never reaches into the previous year.
  `v_position_defense_rank` ranks on **raw yards allowed**, not fantasy points —
  points are league-specific and cannot live in a view.

**Why 2025 first:** Build and verify against a completed season with known
answers. You cannot debug a stats pipeline against a season that hasn't started.

**Then load three seasons, not one** (`make warehouse` → 2023-2025). A single
season misreads anyone who lost time to injury: a 22-year-old with 1,206 yards
as a rookie and four games the next year reads as a washout on current-season
data alone, when he is one of the most valuable assets in dynasty. Three years
also matches the horizon Phase 3b needs. Going back further mostly adds noise.
Prior seasons are **context** — keep them labelled by season and never blend
them into one average.

**Verify against something that is not your own pipeline.** nflverse publishes
its own `fantasy_points_ppr`, which this schema deliberately drops. Recompute
standard PPR from the stored counting stats and diff it across every player-week
of the season. Expect exact agreement; anything else is a column-mapping bug.
This is how `special_teams_tds` was caught going uncounted — 27 return
touchdowns, each a silent 6-point error.

**Done when:** `make ingest SEASON=2025` completes, a test asserts that a
handful of hand-verified 2025 stat lines (pick three players you can look up
manually against a public leaderboard) match the database exactly, and the PPR
cross-check reconciles.

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

**League format detection.** Derive and store these fields on `leagues`:
- `format` — `redraft` | `keeper` | `dynasty` | `survival` | `unknown`.
  **Verify the exact enum values against your own leagues** rather than trusting
  any documented mapping. This is not hypothetical caution: the commonly cited
  0/1/2 mapping is incomplete. Observed against real leagues:

  | `settings.type` | format | how confirmed |
  |---|---|---|
  | 2 | dynasty | two live leagues |
  | 3 | **survival** | one live league |
  | 0 | redraft | documented only |
  | 1 | keeper | documented only |

  `survival` is not a format the roadmap originally anticipated. It was found in
  real data and kept distinct on purpose: a survival league has no persistent
  rosters, so answering it with redraft logic produces confident nonsense.
  Better to recognise the format and decline than to guess.
- **Never trust `settings.type` alone.** Taxi squads and a non-null
  `previous_league_id` are carry-over features, so a league claiming `type=0`
  while having either resolves to `unknown`. Store the raw `settings.type` and a
  `format_source` string alongside the verdict so a wrong call is debuggable.
- If detection is ambiguous, store `unknown` and make the app ask the user
  rather than defaulting silently.
- **You will probably not have every format available to test.** Sleeper is
  dynasty-heavy and many people keep their redrafts on other platforms. Write
  fixtures for the formats you cannot reach and say plainly, in the code, which
  enum values are confirmed and which are inherited from documentation.
- `superflex` — true if `roster_positions` contains `SUPER_FLEX`. Common in
  dynasty and it changes QB valuation more than any other single setting.
- `team_intent` — `contend` | `rebuild` | `balanced`, defaulting to `balanced`.
  User-set, not derived. Stored per league per user.
- Compute and store `available_players`: everyone in the player pool not on any
  roster in the league. This is your waiver-wire universe and it must be derived
  per league, not globally. Exclude taxi and IR players — they are rostered.
- **Cross-walk in both directions.** Sleeper's own `gsis_id` is null for a large
  share of players, including real contributors; nflverse rosters publish
  `sleeper_id`, which fills most of the gaps. Using either alone leaves free
  agents that cannot be joined to the stats warehouse — measured on a real
  league, only 10 of 27 productive free agents resolved from Sleeper's side,
  versus 27 of 27 using both.
- `team_intent` belongs in **its own table**, not as a column on a fetched one.
  It is user-set data, and re-linking a league must not wipe it.
- Retry with backoff; treat Sleeper as flaky.

**Done when:** `make link-league USERNAME=<you> SEASON=2025` (use a real league
you were in) prints your roster, each opponent's roster, and the count of
available free agents — plus the detected format, superflex flag, and any traded
picks. Run it against every league you have and confirm the format field is not
identical across all of them. For any format you cannot reach, hand-write a
fixture; do not proceed to Phase 3 with an untested detector.

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
  one dict, documented, because it will be the source of subtle bugs. Two
  specifics worth writing down before you hit them:
  - **A touchdown is a first down to nflverse, but not to Sleeper.** Any
    first-down scoring key must subtract touchdowns. Left unadjusted it
    over-scores every scoring play by one first down — small, plausible, and
    everywhere. In one real league it was 744 wrong player-weeks, and fixing it
    moved agreement from 66.5% to 97.9%.
  - Keys you cannot compute (a pick-six charged to the quarterback, say) must be
    **reported as unsupported**, not silently skipped. A missing rule is a wrong
    answer, not a rounding error.
- **Validate against points the platform actually recorded, not against your own
  arithmetic.** Sleeper's matchup endpoint publishes its computed points for
  every rostered player every week, which gives thousands of independent checks
  instead of three. Wire it to a command (`make verify-scoring`) and re-run it
  after every edit to the key map — unit tests over hand-written stat lines will
  not catch a bad mapping. Expect high-90s agreement; the residual is a
  stat-source difference (fumbles the two feeds attribute differently), not a
  bug to chase to zero.
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

`LeagueContext` is needed here, one phase before the roadmap originally
introduced it. Build it now, in `advisor/context.py` rather than under `tools/`:
valuation needs the type and sits below the tool layer, so importing upward
would invert the dependency.

**Three rules that are not obvious until the numbers come out wrong:**

- **Floor values at zero.** Replacement level is what the waiver wire gives away,
  so a player below it is worth nothing — not a liability. Unfloored, an aging
  back scores hugely negative and the model recommends paying someone to take
  him. Floor `future` per year, not on the sum: a player who ages below
  replacement in year three is simply off the roster by then.
- **Ignore intent in single-year formats.** Not merely because `future` is 0
  there — applying the weighting anyway rescales `win_now` and makes the same
  redraft player look four times more valuable to a contender than to a
  rebuilder, which is nonsense when the roster resets in January.
- **Apply aging relative to now, not as share-of-peak.** A projection starts
  from what a player is doing today, and today's number already reflects today's
  age. The year-over-year factor is the *ratio* between two points on the curve;
  using the absolute value double-counts the decline and turns a 29-year-old
  back's realistic 24% drop into 56%.

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

Pin the gate to a **named** league. Replacement level differs with team count
and roster shape, so "the first dynasty league" is a test that can silently
change what it proves. And search for the trade pair programmatically rather
than hand-picking one: if only one or two pairs in an entire league produce the
flip, the abstraction is working by luck, and the search itself tells you that.

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

**`LeagueContext` already exists** — it was needed by Phase 3b and lives in
`advisor/context.py`. `load_context(league_id, roster_id) -> LeagueContext`
carries scoring_settings, roster_positions, format, superflex, team_intent, and
the season phase. Resolved once per request and passed down. **This is the only
place format is read.** No tool body, and no schema field, mentions redraft or
dynasty — if a tool needs an `if format == "dynasty"` branch, the valuation
interface is wrong and should be fixed there instead.

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
- **Include the season phase** — current week, or that the season has not
  started, plus which season the underlying stats came from. In March the
  numbers are last season's aged forward, and in week 2 they are mostly last
  season's; a response that presents either as current-year fact invites the
  model to state it as one.
- **Always return `games` alongside season totals.** It is what separates
  "declined" from "was hurt", and without it every rate stat is misreadable.
- Prior seasons are context: label them by season, never blend them into a
  single average. "Averages 700 yards a season" describes nobody.
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

## Phase 5 — Agent loop (local model)

**Goal:** Open-ended conversation in the terminal, running entirely on the
user's own machine.

**Inference is local, and that is a product decision, not a technical one.** A
hosted API bills per token to whoever owns the key, so publishing this to a
league would either charge every leaguemate or put all of it on one person's
card. Running an open-weights model through Ollama makes a question cost $0 no
matter how many people use it, and removes the API key from the codebase
entirely.

**Do not train or fine-tune a model.** The instinct is natural and it is wrong
here. This app was deliberately built so the model needs *no* football
knowledge — every number comes from a tool, and the prompt below forbids
stating any statistic that did not. Fine-tuning would teach it facts it is not
allowed to use, needs thousands of tool-call traces that do not exist, and does
not reliably improve the thing that actually matters: choosing the right tool
and emitting valid arguments. The model's job is narrow — pick a tool, format
JSON, write prose over the result — and 7-8B instruct models already do it.

**Hardware floor:** a 7-8B model at 4-bit quantization is ~5GB on disk and ~6GB
resident, which targets a 16GB machine. That floor is a real constraint on who
can run the app; see Phase 8.

**Build:**
- `agent/backend.py`: a `Backend` Protocol and a `Reply` dataclass. One seam, so
  the loop and the tool layer never learn which runtime is generating tokens.
  Deliberately not shaped like any vendor's SDK.
- `agent/ollama.py`: the only module that speaks HTTP. Translates the registry's
  tool schemas into the runtime's function format — that translation is a
  backend concern and must not leak into `tools/`. Its `health()` check should
  name the exact command that fixes each failure; a runtime that isn't started
  and a model that isn't pulled are essentially every first-run problem.
- `agent/loop.py`: the standard tool-use loop — call the backend, execute every
  returned tool call, append all results, repeat. Hard cap of 8 iterations.
  Catch tool exceptions and return them *as tool results* so the model can
  self-correct (usually by re-resolving a name) instead of crashing the request.
  Small models make more of these mistakes, so this matters more here than it
  would with a frontier model.
- `agent/prompt.py`: the system prompt, **assembled per league** rather than a
  static string.

  **Write it for a small model, which changes the style.** Long explanatory
  prose is what you would write for a frontier model; a 7-8B follows short,
  concrete, imperative rules far more reliably, and every token here competes
  with tool schemas and tool results for a smaller context window. Order the
  rules by how badly breaking them hurts — inventing a statistic is the worst
  failure this app can have, so it goes first. Must include:
  - The current league's name, scoring rules, roster positions, and superflex
    status.
  - Today's date and the current NFL week — **or that the season has not started
    yet**, and which season the underlying stats come from. In the offseason the
    model is reasoning about last year's production aged forward, and it should
    say so rather than presenting it as this year's.
  - An instruction that early-season numbers are anchored to last season and
    carry more uncertainty than the same numbers in December. A week-2 opinion
    stated with week-14 confidence is the most likely way this app misleads.
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
  `/roster`, `/reset`, and a `--verbose` flag that prints every tool call and
  result. That flag is your primary debugging tool for the rest of the project.
- Per-turn accounting: tokens, tool calls, and **wall-clock seconds**. Locally
  the scarce resource is time, not money — a turn takes tens of seconds, so
  latency is the number worth watching.

**Keep session state out of the schemas.** `league_id` and the user's own
`roster_id` are things the model cannot know and will therefore guess — a real
trace had it pass the league's *name* where an id belonged. Bind them in the
loop instead, and pass the live `LeagueContext` into every tool rather than
letting each tool rebuild one from an id. Rebuilding silently resets
`current_week` to "now" and `team_intent` to the default, which answered a
week-14 contending session as a week-18 balanced one and made the whole Phase 3b
intent mechanism a no-op. Every argument removed from a schema is one fewer way
a small model produces an unusable call.

**Flush your progress output.** Local runs take minutes, and Python buffers
stdout when it isn't a terminal — without `flush=True` a working eval is
indistinguishable from a hung one. Hosted inference was fast enough to hide this.

**Done when:** you can hold a multi-turn conversation — ask about a trade,
then follow up with "what if I add my TE?" — and `--verbose` shows sensible tool
calls with no invented numbers.

---

## Phase 6 — Eval harness

**Goal:** Know whether a prompt change made things better or worse — **and pick
the model.**

**This phase moves earlier than its number suggests.** With a hosted frontier
model, quality was a given and evals only caught regressions. Running locally,
model choice is the single largest variable in the system, and it cannot be
settled by reputation or vibes. So build a *minimal* harness (10-12 cases) as
soon as the loop runs, use it to choose among candidates, then finish Phase 5's
prompt tuning against the winner and expand to the full suite.

Pull three candidates in the 7-8B instruct class with documented function
calling and run the same suite against each. Choose on measured tool-call
accuracy and grounding, then **record the numbers in this file** so the choice
is auditable rather than remembered.

**Build:**
- `evals/cases.yaml` — ~30 questions against your 2025 league with known-correct
  answers. Mix of: simple lookups, comparisons, trades, waiver questions,
  ambiguous player names, questions with no valid answer (a player who doesn't
  exist), and adversarial ones ("just tell me Bijan is a bust").
- **A `grounded` assertion on every case: each number in the answer must appear
  in a tool result.** This is the most valuable check in the suite and the one
  most specific to this app — its entire premise is that numbers are traceable,
  and unlike tone or length, fabrication is mechanically detectable. Allow
  rounding (a model reading "23.44" as "23.4" is correct, not inventing) and
  exempt years and small counts, which are legitimately reasoned about.
- **Assert on behaviour, not vocabulary.** "does not match any player" and
  "couldn't find that player" are the same correct answer; a `must_say_any` list
  tests the model, while a single required phrase tests its word choice.
- **Every trade and roster-construction case runs twice — once per format** —
  with a `expects_different_answer: true` flag. The single highest-value eval in
  the suite is the paired age-for-production trade: if the redraft and dynasty
  answers come back the same, something upstream collapsed. Add dynasty-only
  cases too: a pick-for-player trade, "should I sell my 29-year-old RB," and
  "which of my young guys should I hold through a rebuild."
- Each case asserts on checkable properties, not exact wording: which tools were
  called, whether a specific number appears, whether a recommendation was given,
  whether it hallucinated a stat.
- **Run a subset at several points in the season** — week 1, week 8, and an
  offseason context — using the same questions. Answers should differ in
  confidence and basis, not collapse or contradict themselves. This is the
  cheapest way to catch a regression in the Phase 3c shrinkage.
- `make eval MODEL=x` prints a pass/fail table, tool-call accuracy, grounding
  rate, and wall-clock time. Print live per-case progress with an ETA — a local
  suite runs 15-20 minutes, long enough that silence is indistinguishable from
  a crash.
- **Do not run `make test` while an eval or chat session is live.** DuckDB takes
  an exclusive lock, so the suite fails with a hundred confusing connection
  errors that look like a regression and are not.

### Model chosen: qwen3:8b (2026-08-12)

Twelve cases, seed pinned, 8192-token context, same league and week for all
three:

```
model                  passed  tools ok  grounded  fabricated  slow    min
--------------------------------------------------------------------------
qwen3:8b                11/12     12/12     100%           0     1   15.0
llama3.1:8b              9/12      9/12     100%           0     0    2.4
hermes3:8b               3/12      4/12      92%           1     0    0.9
```

**qwen3:8b** wins on the criteria that decide it: perfect tool selection and no
invented numbers. Its one failure is `waiver_wire` ("no recommendation given"),
reproducible across two seeded runs, and `start_sit` answers correctly but takes
342s — both the same behaviour, surveying instead of deciding on open-ended
roster questions. That is a prompt problem, and it is the next work.

**llama3.1:8b is the honourable mention and the fallback.** Also zero
fabrications, and **six times faster** (2.4 min vs 15.0 for the same suite) —
qwen3 spends its time in a reasoning block that this app mostly does not need.
It loses on tool discipline: it calls `compare_players` without `resolve_player`
first, and sometimes writes a tool call as prose in its answer instead of
emitting one. If the 342s `start_sit` proves unfixable, revisit this trade.

**hermes3:8b is unusable here**, and not because it is weak. It asks the user
for the data instead of calling tools — "could you provide the player_ids",
"provide me with your league ID". Those are exactly the session values kept out
of tool schemas on purpose (the model cannot know them, so the loop binds them).
A model that responds to their absence by interrogating the user cannot work in
this architecture, whatever its benchmark scores say.

Re-run any of this with `make eval-compare MODELS=...`; finished models are
reused from `evals/results/` unless you pass `--fresh`.

### Season phase, and a regression I caused by fixing what wasn't broken (2026-08-12)

Pairing generalised: cases can declare `paired_weeks: [0, 14]` as well as
`paired_formats`. Season phase derives entirely from `current_week`, so moving
the week exercises every Phase 3c branch without waiting for the calendar —
including the offseason, where dynasty leagues actually trade hardest and which
nothing had ever tested.

Phase 3c works, now confirmed rather than assumed:

- **Week 2** — *"Given the small sample size and the fact that these numbers are
  anchored to 2024, it's too early to be overly concerned."* The shrinkage
  reaches the user as stated uncertainty rather than a confident week-2 read.
- **Week 18** — `_win_now_caveat` earns its place. Shown `win_now: 0` for an
  elite receiver, the model does not call him worthless. That was defensive code
  nobody had verified.

**A harness bug first:** `_paired_format_failures` ran on week-paired cases and
read the label `week0` as a format name. None of the week labels is a multi-year
format, so a dynasty league was failed for having exactly the future values it
should have. Guarded. That is the eighth measurement bug of the day and they all
share a shape — **asserting on a label rather than on what the label denotes**.

**Then a regression that was entirely mine.** Reading the offseason answer, I
noticed *"Given the league's rebuild intent, his win-now value is more
critical"* — backwards for a rebuild. So I gave `compare_players` an
`intent_weighted_value`, by symmetry with `evaluate_trade`. It regressed
`age_for_production_paired` from three straight passes to a fail: the model met
a per-player weighted value AND a trade delta in one turn, and fell back to raw
deltas. Reverted, and the reason is recorded in the code so nobody re-adds it.

**The symmetry is the trap.** `evaluate_trade` already answers "is this trade
good"; a second weighted figure competes with it rather than helping.

The deeper mistake was method, not code: that change fixed an **observation**,
not a failing case. Three of the four regressions today came from editing
without a failing test in front of me. So the gap is now a case —
`intent_direction_on_compare` — and the next attempt at it gets measured instead
of eyeballed.

**18/18, 100% grounding, 9.0 min.**

### Format pairing, and a wrong answer that passed everything (2026-08-12)

Cases can declare `paired_formats: [dynasty, redraft]`. The runner asks the same
question under each, varying ONLY `format` via `dataclasses.replace` — holding
roster, scoring, week and player pool fixed, so a difference is attributable to
format and nothing else. A second real league would vary all of them at once and
prove nothing.

Format branching works end to end, proven with numbers:

```
dynasty  tools returned future 113.97 / 131.5  -> "...intent-weighted net gain
         of +6.46 for your rebuild-focused team. Go ahead with the trade."
redraft  tools returned future 0.0             -> "...decrease your win-now
         value by 37.8 points. Do not trade."
```

**But `expects_different_answer` is not enough on its own — two answers can
differ from each other and both be wrong.** The first paired run passed every
assertion in the suite while concluding the opposite of its own reasoning:
"prioritise future value", having just noted the trade *raised* future by 17.53,
then "Keep McCaffrey and avoid the trade". So cases now carry
`correct_answer_by_format`, and the right answer is not an opinion — the app's
own weights decide it, and they invert:

```
rebuild intent = 0.20 win_now / 0.80 future
  dynasty  McCaffrey 101.82  vs  McMillan 108.28  -> accept
  redraft  future is 0, so win-now only: 53.2 vs 15.4 -> decline
```

**The cause was a tool telling the model to do the wrong thing.** `evaluate_trade`
already returned `intent_weighted_delta: 6.46`, and its `no_verdict` field said
"weigh win-now against future using the league format and team intent above" —
instructing the model to redo, by hand, arithmetic already in the same payload.
It compared raw magnitudes (-37.8 against +17.5), and declined. Three fixes, only
one of them a prompt change:

1. `no_verdict` now points at the number instead of asking for it again.
2. The weighted figure comes FIRST in the payload; a small model combines
   whatever it meets first.
3. Prompt rule: *when a tool gives you a combined or weighted number, decide with
   that number; do not re-derive it from the parts.* That is a statement about
   this architecture, not a patch for one case.

**Intent leaked through the envelope.** Every tool response carries `team_intent`
by design, so a redraft answer argued "since your team is rebuilding" — meaningless
where the weights are (1.0, 0.0). The envelope now carries
`team_intent_does_not_apply` for single-year formats, matching the existing
`_win_now_caveat` pattern. Partial fix: the model stopped *concluding* from it,
but still mentions it.

Result: **14/14, 100% grounding, 6.7 min.**

### Thinking is OFF, and that was measured (2026-08-12)

qwen3:8b is a reasoning model. Same prompt, same seed, same league:

```
thinking OFF: 12/12  100% grounding   4.3 min
thinking ON : 11/12  100% grounding  15.1 min
```

**Off is better on both axes** — more accurate *and* 3.5x faster, which is not
the intuition. The single case thinking-on fails is `trade_eval`, where it calls
`compare_players` instead of `evaluate_trade`: the reasoning block talks itself
into "compare these two players" rather than "price this trade". That is a real
ambiguity in the tool descriptions — `compare_players` advertises itself as the
tool for "the 'is he better than' half of a trade discussion" and never says
where it stops — but it only surfaces when the model reasons its way there.

`THINKING=true` (or `eval --thinking`) turns it back on. **Re-check this on any
new model**; it is a property of qwen3, not a law.

### The two Phase 5 defects, and what they actually were (2026-08-12)

Both were logged as "the model is too verbose". Neither was.

**`waiver_wire` — no defect at all.** The model answered *"The best available RB
is Ray Davis... Prioritize Ray Davis"*, and `wants_pick` scored it as giving no
recommendation because none of its fifteen marker words appear in that sentence.
The check is now behavioural: name a player that came back from a tool, and do
not hedge without committing. That ties it to the data rather than to phrasing,
the same instinct as the grounding assertion — and this file already said
"assert on behaviour, not vocabulary" while the code did the opposite.

**`start_sit` — 342s to 14s, two unrelated causes.** Most of it was the thinking
block. The rest was the word FLEX: it sits in the prompt's `Starters:` line
looking exactly like QB or RB, so the model searched the waiver wire for a
player whose *position* was FLEX, then for RB, WR, TE and K in turn, and hit the
tool-call cap. It already had the roster on call one. The prompt now says what
the word means, and the case went from **8 tool calls to 1**.

**A prompt fix that made things worse, kept here because the lesson is the
point.** To stop the model answering from football memory, the prompt gained
"if you have not called a tool, you have nothing to say" — which a model reads
as *more tools are safer*. `start_sit` then looped `get_available_players` six
times and hit the cap. The rule now targets the actual failure ("every claim
must come from a tool result you have already received") without implying volume
is a virtue. No amount of re-reading the prompt would have caught this; the
suite caught it in one run.

**The failure worth remembering:** with thinking off, `adversarial_premise` had
been answering *"Bijan Robinson is not a bust; he has shown consistent
production with a strong rushing attack"* — **zero tool calls, pure football
memory** — and still scored **100% grounding**, because the audit checks
invented *numbers* and that sentence contains none. Prose claims evade it
entirely; `expect_tools` is what caught it. The prompt now forbids claims in
words as much as in figures. The hole existed with thinking on too — reasoning
was hiding it, and only the adversarial case was pointed enough to expose it.

### What the first real runs taught (2026-08-12)

The first baseline scored **6/12 with 70% grounding**, and almost none of that
was the model. Recorded here because each one is a way an eval harness lies to
you, and the lie always looks like a model problem.

**1. The runtime silently truncated every long turn.** Ollama defaults to a
4096-token context window and does not error when a request exceeds it — it
drops tokens and answers from what is left. The fixed overhead here is ~2k
tokens (six tool schemas ~1289, system prompt ~700) before the user has asked
anything, and one `get_my_roster` result is ~1387 more. Three cases were
overflowing into a context-shift thrash and hitting the 300s HTTP timeout.
Setting `num_ctx` to 8192 took the suite from 6/12 in 23.8 min to 10/12 in
13.9 min, and turned `my_roster` from a 308s timeout into a 62s pass.
*Symptom of the bug: slow wrong answers, never an error.*

**2. Backend failures were scored as fabrications.** A timeout message contains
the port (11434) and the timeout (300). The grounding audit read those as
invented statistics, so an infrastructure failure was indistinguishable from a
lying model — and would have sent us tuning the prompt. Backend errors are now
their own category, reported as **NOT MEASURED**, never as a model failure.

**3. Correct unit conversion was scored as fabrication.** `compare_players`
returns `snap_share: 0.785`; the model wrote "78.5% snap share" and was marked
as inventing a number. Percent conversion is reading, exactly like rounding.
Corrected, **qwen3:8b fabricated nothing across all 12 cases** — the opposite of
what the first run reported.

**4. Wall clock is not a pass/fail criterion.** `start_sit` "passed" in 288s,
twelve seconds under the timeout — correct answer, unusable feature, invisible
in the report. But gating on time proved worse: the same model at the same seed
ran 3-5x slower while the laptop was busy (`ambiguous_name` 30s → 136s). A time
gate fails cases for being unlucky. Slowness is now reported (`SLOW`) and ranked
on, never failed on.

**5. Pin the seed for evals, never for chat.** Temperature 0.1 is not
deterministic, and a 12-case suite is small enough that noise flips individual
cases in both directions — survivable when evals catch regressions,
disqualifying when evals *pick the model*. Chat leaves the seed free, because a
user who rephrases a question deserves a different answer.

**6. Save each model's run as it finishes.** Three models is ~45 minutes; a
laptop closing during the third should not cost the first two. `eval-compare`
writes to `evals/results/` per model and reuses finished runs, rejecting any
cached run whose case count no longer matches.

**7. A strict tool signature scored our bug against a model.** llama3.1:8b sent
`weeks="8"`, which reached `min(weeks, MAX_WEEKS)` and raised a TypeError. Three
of its twelve cases died there and read as model failures. Arguments are now
fitted to each tool's declared types before dispatch (`coerce_arguments`), which
took it from 8/12 to 9/12 — a number that is finally about the model. A small
model getting the type *wrapper* wrong is not the same as getting the call
wrong, and rejecting it throws away work the model got right.

The through-line: **an eval harness needs its own tests.** Every one of these
made a working system look broken or a broken measurement look authoritative.
Six of the seven initially presented as "the model is bad at this."

**Done when:** the suite runs in one command and you have a baseline score. From
here on, no prompt or tool change ships without re-running it. This is the part
of the project that actually teaches you agent engineering.

---

## Phase 7 — Local web UI

**Goal:** A nicer interface than the terminal, still entirely on the user's
machine.

**Build:**
- FastAPI bound to `127.0.0.1`: `POST /chat` (streaming via SSE),
  `POST /link-league`, `GET /health`, `GET /data-status`.
- Conversation persistence in DuckDB: `conversations`, `messages`. Load history
  on each request — the HTTP layer is stateless, the database holds state.
- Single-page frontend: one HTML file, vanilla JS, streaming chat. No build step,
  no framework. Served by FastAPI as a static file. Resist scope creep here.
- **Stream the response.** A local turn takes tens of seconds; a spinner that
  long feels broken, where the same wait with tokens appearing does not. This
  matters more here than it would against a fast hosted API.

**No public URL, no auth, no Docker deploy.** Binding to localhost is what keeps
the app free — the moment it serves other people from one machine, someone is
paying for that machine's GPU.

**Done when:** a fresh clone plus `make serve` gives a working chat app at
`localhost:8000`, with no API key of any kind.

### Choosing a league (2026-08-13)

"How will I know what league the chat is looking at?" had no good answer: the
page never showed it and offered no way to change it, and `/chat` accepted a
`league_id` the frontend never sent. Adding the picker surfaced two real bugs.

**A conversation drifted between leagues.** `conversations.league_id` was
written at creation and never read back, so turn two re-ran the default picker —
and that default moves when a league is linked, an intent is set, or a league is
renamed. History about league A stayed in the prompt while the tools and system
prompt described league B. Threads are now pinned via `conversations.league_of()`,
and switching starts a new one. No migration: the column was always populated.

**An unknown league killed the stream mid-response.** A bogus id passed
`_pick_league` unvalidated, then `load_context` raised inside the response
generator — after a 200 had already gone out, which a browser can only render as
an answer stopping mid-sentence. `_pick_league` now validates, `load_context`
runs in the handler, and the whole class returns a 404.

**One ordering, one picker.** `context.list_leagues()` is now the single
definition of "which league am I in"; `_pick_league` takes row zero and the UI
renders the same list, so the dropdown and the default cannot disagree. Its
`team_intent` join is aggregated — that table is keyed `(league_id, roster_id)`,
so a plain join fanned one league into a row per intent and `LIMIT 1` chose
between them arbitrarily.

Also replaced a test of mine that asserted `"survival"` appeared in
`_pick_league`'s **source text** — the same "assert on the label, not the thing"
weakness that caused several eval-harness bugs. It now asserts a survival league
is not offered first.

### Built (2026-08-12)

`make serve` → `http://127.0.0.1:8000`. `GET /health`, `GET /data-status`,
`GET|DELETE /conversations`, `POST /chat` (SSE). One HTML file, vanilla JS, no
build step and no outbound requests — asserted by a test, so a CDN link cannot
creep in.

**Streaming is real, not cosmetic.** `run_turn` blocks, so `/chat` runs it on a
thread and drains a queue; the first version collected tokens and emitted them
at the end, which is a spinner wearing a stream's clothes — the user still waits
the whole turn. Tool events stream too, because "which numbers came from where"
is this app's premise, not decoration.

`OllamaBackend.chat_stream` was added beside `chat`, both parsing responses
through one `_to_reply` so a tool call cannot be read one way streamed and
another way not. The loop streams only when someone passes `on_token` AND the
backend offers `chat_stream` — the `Backend` protocol still guarantees only
`chat`.

**Two bugs from inventing identifiers**, both caught by running the SQL rather
than reading it: a `rosters` table (it is `league_rosters`) and a `rows` column
(it is `row_count`). Fixed by reusing the CLI's `_pick_league` instead of
writing a second league picker, and by a test that executes `/data-status`
against the real schema.

**The module is `advisor.web.server`, not `app`.** With both a module named
`app` and a re-exported FastAPI instance named `app`, `advisor.web.app`
resolved to the instance and monkeypatching failed confusingly.

---

## Phase 8 — Packaging and distribution

**Goal:** A leaguemate can install this and use it, and it costs nobody anything.

Local-first inference turns this phase from a deployment into a packaging
problem, and deletes most of what it used to contain:

- **No cloud host.** A $5/mo box cannot run a 7-8B model; GPU hosting is
  $50-300/mo and would simply move the bill rather than remove it.
- **No Postgres migration.** That existed only to serve a cloud deployment.
  DuckDB is the permanent answer. (The `db.py` repository discipline stays good
  design regardless — it just isn't load-bearing for a migration anymore.)
- **No rate limiting, no token budgets, no auth.** There is no invoice to run
  up and nothing exposed to the internet.

**Build:**
- A setup script that installs Ollama, pulls the chosen model, builds the
  warehouse (`make warehouse`), and links a league. First run downloads ~5GB,
  which is the one genuinely rough edge — tell the user before it starts, not
  during.
- Weekly refresh: a cron entry or a documented Tuesday-morning `make ingest
  SEASON=2026` after Monday Night Football, plus re-pulling league rosters.
- A hardware note in the README. **~16GB RAM is a real floor**, and a leaguemate
  on an 8GB machine will have a bad time. Say so honestly up front rather than
  letting them discover it after a 5GB download.

**Done when:** a leaguemate on their own laptop can go from `git clone` to a
working answer about their roster, and the total cost to everyone involved is
$0.

### Built (2026-08-12)

`make setup` and `make refresh`, both idempotent, both `set -euo pipefail` so a
failed step stops rather than reporting success over a half-built install.

**Setup states the cost before spending it.** Size (~5.5GB, nearly all model),
every step it will take, and a RAM check against the 16GB floor — all printed
*before* the confirmation prompt, per this phase's own instruction. Below the
floor it explains what will happen (swapping, minutes per answer) and suggests a
smaller model rather than refusing.

**Two bugs found by running the scripts rather than reading them:**

1. *Setup was not idempotent.* Its "is a league already linked?" check grepped
   `make status` output for the word "league", which that output never contains,
   so a configured machine would be re-prompted every run. Both checks now query
   the database. This is the same mistake as asserting on a label — parsing a
   human-readable table is not a contract.
2. *`make refresh` 404'd in the offseason.* It defaulted to `date +%Y`, but an
   NFL season is named for the year it **starts** and opens in September, so in
   August it asked nflverse for a 2026 file that does not exist and dumped a
   stack trace. Now derives the season properly and, if a season genuinely has
   no published stats yet, says so and leaves the warehouse alone.

**The username is stored in `.env`, not inferred.** `league_users` lists every
manager in a league and marks none of them as you, so `setup.sh` records
`SLEEPER_USERNAME` after linking and `refresh.sh` reads it back.

**The fresh-clone test found a bug nothing else could.** Run for real, an empty
checkout answered *"How does my roster look?"* with **"I need your `roster_id`.
Please provide it"** — a question the user cannot sensibly answer and one the
loop exists to bind for them. Two causes, both invisible on a developed machine:

1. `roster_id` came only from `team_intent`, which a fresh install has no rows
   in. Sleeper already knows: `league_rosters.owner_id` joins to
   `league_users.user_id`, whose `display_name` is the username. `_pick_league`
   now falls back to the owned roster, fixing the CLI, the web UI and the gate
   at once.
2. It defaulted into the **survival** league, where persistent rosters do not
   exist — so a newcomer would land where most of their questions do not apply.
   Now deprioritised in the ordering.

Neither could appear on the development machine, where a `team_intent` row had
been supplying `roster_id` all along.

**Gate met after the fix**: an empty checkout with no `.env` and no `data/`
reaches a grounded answer about the right roster in the right league through
`make setup` alone.

`tests/test_packaging.py` asserts the properties a newcomer depends on — scripts
parse, the size warning precedes the prompt, the hardware floor is stated, every
`make` target is documented, and **no API key appears anywhere in `src/` or
`scripts/`**. That last one is the claim the whole project rests on, so it is
asserted rather than trusted.

### Built (2026-08-13) — linking a league from the browser

Setup used to require a terminal: `make link-league USERNAME=<you> SEASON=2025`,
with a username you had to know to supply and a league id you had to look up. The
header now has **Find my leagues** — type a Sleeper username, see every league
that account plays in, pick one.

**Looking up writes nothing; linking writes.** `GET /sleeper/leagues` calls
Sleeper and returns what it finds without touching the database, so a typo costs
one request rather than a half-ingested league. `POST /leagues/link` does the
ingest, and only for the league that was chosen — linking everything found would
put a league the user did not ask about ahead of the one they did.

**Format is shown while choosing, not after ingesting.** `detect_format` runs on
the raw Sleeper object, so the list reads *dynasty · superflex · 12 teams* before
anything is committed. That is the field that changes the advice, so it is the
field that should inform the choice.

**Who you are moved into the database.** `sleeper_account` joins `team_intent`
and `conversations` as user-set data in its own table. It has to be there rather
than only in `.env`, because `get_settings()` is `lru_cache`d — an env-only value
would not take effect until the server restarted, which is a strange thing to
require of someone who just typed their name into a box. `link_leagues()` writes
it, so the CLI path records it too, and ownership now matches on `user_id` first
so a rename on Sleeper does not orphan a roster. Verified with `SLEEPER_USERNAME`
blanked entirely: link a league in the browser, and the next turn resolves the
right roster with no `.env` and no restart.

**The page is a setup surface now**, so the app creates its schema at startup —
`make serve` on a fresh clone must not open onto a page whose every request 500s
on a table nobody has created. Same reasoning one level down: `account.py` runs
its own `IF NOT EXISTS` DDL (from `schema.py`, so there is one definition) before
reading, because every warehouse built before this feature lacks the table and
`list_leagues()` runs on *every* league lookup in both interfaces.

**A bug found by re-reading, not by a test.** `loadLeagues()` returned early on
the empty case, so `if (body && !body.leagues.length) openSetup()` never fired —
the panel would not have opened on the fresh install it exists for. Nothing here
type-checks: `tests/test_web.py` now asserts every `getElementById` in the page
matches a real `id`, which catches the class of failure where a mistyped id
silently returns null and the button it belonged to is simply dead.

### Built (2026-08-13) — three columns: your roster, the chat, the league

The chat could always tell you about your roster; you just could not *look* at
it. Now the page is `roster | chat | league`: your team on the left, and on the
right a tabbed browser for **Teams**, **Waivers** and **Standings**. Both columns
toggle from the header and start closed on a narrow window rather than squeezing
the chat into a gutter.

**The panels and the answers cannot disagree.** Everything renders from the same
primitives the tools use — `player_index` for scoring, `get_valuation` for value,
`rosters.player_entry` for the shape (promoted from `_player_entry` for exactly
this). `tests/test_panels.py` asserts the agreement field by field rather than
trusting it, and `waivers.available_candidates` was extracted so the wire the
browser shows and the wire the model reads are one list.

What the panels deliberately do **not** reuse is the tool wrappers. Those cap
lists and shrink payloads to fit an 8k context; a browser is not a context
window, so the panel shows 40 free agents where the model gets 15, and a whole
31-player dynasty roster where the model gets what fits.

**The lineup is real.** Sleeper's `starters` array is positional against
`roster_positions` minus BN/IR/TAXI, so each starter is labelled with the slot it
fills — and `"0"`, Sleeper's empty slot, stays in place, because dropping it
shifts every player below into the wrong slot. When the lengths disagree the
labels are omitted entirely: a mislabelled slot is a wrong answer wearing a UI.

**Two presentation decisions that are really honesty decisions.** Valuations are
floored at replacement level, so only 6 of 27 players on a real roster have a
nonzero `future` — correct, but a column of zeroes reads as a verdict on the
other 21, so a value appears as a chip only where there is one. And rostered
players with no stats (rookies, call-ups: four of them on the test roster) are
named and labelled rather than silently dropped, which is right for a token
budget and looks broken in a panel.

**One bug and one hazard, both found by running it.** `/panel/roster?roster_id=N`
rebuilt the context around the roster being *viewed*, so an opponent's team came
back `is_you: true` and would have been valued under *their* `team_intent` —
the rebuild-from-an-id hazard this project already documents, arriving through a
new door. Fixed by keeping "who is asking" and "what to display" separate, as
`get_my_roster` always did. Separately, `players._available_seasons` is an
`lru_cache` over database state: a temp database with no stats poisoned it and a
*later* test against the real warehouse read `stats_season: None`. Now dropped
between tests by an autouse fixture.

**The survival league finally got exercised**, incidentally — it was the one
format never driven end to end. It renders: 8 starters, no bench, and standings
that refuse to present sixteen 0-0 records as a table.

### Built (2026-08-14) — rosters that are current, not weekly

`make refresh` pulled rosters once a week, which meant the app could confidently
recommend starting a player dropped on Tuesday. Now every league load — page
load, league switch, `make chat` startup, `/league N` — pulls the league's
rosters, managers, traded picks and free-agent pool from Sleeper first, and the
panels render after it. Stats are untouched: nflverse publishes weekly and that
is still `make refresh`'s job.

The whole question was whether this could live in the page-load path. Measured
against three real leagues it costs **0.27s** when nothing has changed and 1.6s
when something has, from three decisions:

- **The four Sleeper calls run in parallel.** They are independent, each a ~0.4s
  round trip, and sequentially they were 1.5s of the 1.6s.
- **Nothing is written when nothing changed.** A fingerprint of the fetched data
  is compared with the last one, and most leagues on most loads skip the rewrite
  entirely. The fingerprint covers *what we would store*, not what was received
   — hashing raw responses would let a read marker or a counter look like a
  trade, and then every refresh rewrites everything, which is the cost this
  exists to avoid.
- **`ingest_league` takes the data it was given.** It re-fetched users, rosters
  and picks itself, so a changed league would have cost seven requests where
  four do.

**Failure is a fact, not an exception.** `refresh_league` never raises. Sleeper
unreachable returns `ok: false` with a reason and the panels render the saved
rosters, because the warehouse copy is a complete working answer that is merely
older — and an app whose premise is local-first cannot fall over when a
third-party API blips. Sleeper answering `null` for a league (its idiom for
"gone") must not delete a working league either.

**Two states worth naming.** `available_players` is a delete-and-rewrite, so a
reader mid-refresh could see an empty wire; skipping unchanged leagues means
that window almost never opens. And the fingerprint claims "a rewrite would be a
no-op", which is only true if the last write *finished* — an ingest interrupted
between the DELETE and the INSERT would otherwise leave an empty wire that every
later refresh politely skips, stuck with nothing saying why. `_looks_loaded`
checks the data is really there before trusting the fingerprint.

Deliberately **not** in `_pick_league`: `eval` and `eval-compare` resolve leagues
through it, and a network call there makes a benchmark depend on the weather.
`make chat --no-sync` opts out for offline use.

### Built (2026-08-14) — every rostered player has a name

Two players in Beer Ball Empire rendered as **"(unknown player)"** on their own
manager's roster: Tyrone Broden and Henry Ruggs, both without an NFL team.

The cause is a source mismatch that had been there since Phase 1. `players`
comes from nflverse, which publishes *NFL rosters* — so it lists only people on
a team that season. Dynasty managers stash exactly who that excludes: practice
squad, suspended, injured out for the year, out of the league entirely. Sleeper's
dump has all 12,218 of them and we were downloading it every week, filtering it
down to the fantasy pool for the waiver wire, and throwing the rest away.

`sleeper_players` now mirrors the dump **unfiltered**, and `refresh.identify()`
is the one lookup: the mirror, then the nflverse crosswalk, then the cached dump
on disk. Across all three linked leagues, unidentified players went from 2 to 0.

**The third fallback is load-bearing, not belt-and-braces.** Importing 12k rows
costs ~4s — DuckDB is columnar and row inserts are slow, and `executemany`
measured *worse* (7.7s) — so the mirror is built only where seconds are already
being spent: `link_leagues`, and the branch of a refresh that was writing anyway.
That means it is legitimately behind on a fresh install and in the week the dump
updates. Reading the cached dump costs 0.08s, so identity never waits for the
optimisation.

**The label improved with the data.** "(unknown player)" became the name, and
"no 2025 games" became the specific reason — *no NFL team*, or the Sleeper status
— which is the half a manager can act on.

**And the model says the same thing.** `get_my_roster` counted these players
("2 rostered players had no stats") where the panel beside it now shows names;
it lists them under `rostered_without_stats` through the same lookup. Fixed one
adjacent bug on the way: `"0"`, Sleeper's empty starting slot, was being counted
as a player without stats and inflating that number.

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
- **Development upside for young players.** `future` ages a player's *current*
  production, so a 22-year-old already below replacement gets zero future value
  — which under-rates exactly the unproven breakout candidates a rebuilding team
  cares about. Modelling it properly means projecting improvement, not just
  decay. Revisit when Phase 6 evals show it costs real answers.
- **Injury context.** `games` played is a decent proxy for "was hurt", and
  nflverse ships an injuries dataset if a better one is needed. Not worth
  ingesting until the tools show they miss it.
- **Chasing scoring agreement to 100%.** The last ~1% against Sleeper is a
  stat-source difference (fumbles the two feeds attribute differently), not a
  mapping bug. Diminishing returns.
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
| 2 | 3, 3b, 3c |
| 3 | 4, 5 (+ a minimal Phase 6 to choose a model) |
| 4 | 6 in full, 7, then 8 |

Phase 3b is the one that will run long — the aging curves and pick values are
fiddly and worth getting right. If you're behind, ship redraft-only through
Phase 5 (`RedraftValuation` alone, dynasty leagues rejected with a clear message)
and add `DynastyValuation` during the season. The interface makes that a
genuinely additive change rather than a rewrite, which is the whole point of
splitting it out.

**Do not defer Phase 3c past Phase 4.** It changes what every projection means,
and tool output and prompts tuned against mid-season behaviour would have to be
revisited once the basis shifts underneath them. It is also short — the work is
mostly deleting assumptions.

Phases 0–4 are the load-bearing work and involve no LLM calls. If you run short
on time, ship Phases 0–5 as a local CLI tool for yourself in week 1 of the
season and add the web layer during the bye weeks.
