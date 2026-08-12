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
                    DuckDB file (local)  →  Postgres (Phase 8)
```

`LeagueContext` sits **below** the tool layer, not inside it: valuation needs the
type, and importing upward would invert the dependency. It lives in
`advisor/context.py` and carries the season phase (`current_week`,
`stats_season`, `is_offseason`) as well as format and intent — see Phase 3c.

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
    .env.example          # ANTHROPIC_API_KEY, DB_PATH, DEFAULT_LEAGUE_ID
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
- **Run a subset at several points in the season** — week 1, week 8, and an
  offseason context — using the same questions. Answers should differ in
  confidence and basis, not collapse or contradict themselves. This is the
  cheapest way to catch a regression in the Phase 3c shrinkage.
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
| 3 | 4, 5 |
| 4 | 6, 7, then 8 |

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
