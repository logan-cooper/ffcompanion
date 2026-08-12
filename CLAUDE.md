# CLAUDE.md

## Project
Fantasy football AI advisor. Full plan: docs/ROADMAP.md — read it before
starting any phase.

**Status: Phase 5 complete, Phase 6 model choice made** (warehouse 2023-2025,
Sleeper leagues, scoring engine, format-aware valuation, year-round validity,
six-tool layer, local agent loop + eval harness). **The model is qwen3:8b** —
11/12, 12/12 tool selection, 100% grounding; the evidence and the runners-up are
in docs/ROADMAP.md Phase 6. Next: the prompt, against two measured defects —
`waiver_wire` gives no recommendation, and `start_sit` answers correctly but
takes 342s. Both are the same behaviour: surveying instead of deciding on
open-ended roster questions. Don't skip ahead — each phase has a "Done when"
test that gates the next one.

## Stack
Python 3.12, uv for deps, DuckDB locally (Postgres in Phase 8), FastAPI
(Phase 7+). The agent loop runs a LOCAL open-weights model via Ollama —
no API key, no per-token cost, anywhere in this project.

## Hard rules
- All database access goes through src/advisor/db.py, which exposes exactly
  get_conn() and query() — no direct DuckDB imports anywhere else. query()
  handles writes too and returns [] when there's no result set.
- Tools (src/advisor/tools/) return data only, never prose or opinions, and
  evaluate_trade returns NO verdict. Every response carries the same envelope
  (format, intent, season phase, stats_from_season, data_as_of) — the model
  cannot be trusted to remember the system prompt twelve turns later.
- Tools NEVER branch on format. Ask get_valuation(ctx) and report what it
  returns; signatures must be identical in redraft and dynasty. `make
  tools-demo` runs all six under both with the same args to prove it.
- Label zeroes that could read as verdicts. With no games left win_now is 0 for
  everyone by arithmetic; unlabelled that looks like a judgement on the player.
- Truncate by PRIORITY, not by list length. Dropping whichever list is longest
  removes starters from a deep roster, and a lineup question needs them.
- The model is LOCAL and small (7-8B). Never suggest a paid API. Two rules
  follow: keep session state (league_id, the user's roster_id) OUT of tool
  schemas — the model can't know those values and will guess, so the loop binds
  them; and write prompts short, ordered, and imperative rather than as prose.
- Never let a tool rebuild its own LeagueContext from an id. Pass the session's
  ctx. Rebuilding resets current_week and team_intent to defaults and silently
  answers the wrong question.
- Don't run `make test` while `make chat` or `make eval` is live — DuckDB's
  exclusive lock makes the suite fail with ~100 errors that look like a
  regression and aren't.
- Local inference takes tens of seconds per turn and 15-20 min per eval suite.
  Always flush progress output, and send it to STDERR — progress on stdout means
  `eval --json > file` swallows every sign of life and a working run is
  indistinguishable from a hung one.
- Ollama's default context window is 4096 tokens and it does NOT error when a
  request exceeds it — it silently drops tokens, so the symptom is a slow wrong
  answer, never a clear failure. The six tool schemas (~1289 tokens) plus the
  system prompt (~700) plus one get_my_roster result (~1387) is already ~3.4k
  before the model writes anything. num_ctx is set explicitly in
  src/advisor/agent/ollama.py and floored at 8192; that floor fits the 16GB
  target machine (~0.9GB of extra KV cache).
- An infrastructure failure is not a model failure. A timeout message carries
  the port (11434) and the timeout (300), which the grounding audit reads as
  invented statistics — so a runtime problem disguises itself as a fabricating
  model and sends you tuning the prompt. CaseResult.infrastructure_error keeps
  them apart; a case that errored is NOT MEASURED, not failed.
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
  (It does NOT survive deleting data/advisor.duckdb — re-run set-intent.)
- get_valuation(ctx) in src/advisor/valuation/ is the ONLY place format is
  branched on. If a tool needs `if format == "dynasty"`, the interface is wrong
  — fix it there. Tool signatures must be identical in both formats.
- PlayerValue always carries BOTH win_now and future (future=0 in redraft).
  Keep them separate all the way to the response so the model can state the
  tradeoff; collapse only via intent.combined_value, and only at the end.
- Valuations are floored at zero. Replacement level is free from the wire, so a
  worse player is worth nothing, never negative — otherwise the model
  recommends paying someone to take an aging star.
- The app must work ALL YEAR, not just mid-season. Never assume the current
  season has data: dynasty leagues trade hardest Feb-Aug, when the season being
  valued has zero games. Resolve player identity via players.player_profile()
  (falls back to the latest season with data, ages forward); read the season
  phase off LeagueContext (is_offseason / current_week / stats_season), never
  from a hardcoded default.
- Projections shrink toward a prior-season baseline by sample size (K=6 games).
  One formula, no season-phase branching. The prior baseline is a FLAT season
  mean on purpose — recency predicts next week, not next year, and week 18 is
  full of rested starters.
- Aging is relative, not absolute: use relative_multiplier(), which is the
  RATIO of curve points. Today's production already reflects today's age;
  multiplying by the absolute curve value double-counts the decline.
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
make verify-scoring       # scoring vs points Sleeper actually recorded
make tools-demo           # all six tools, dynasty vs redraft, same args
make chat                 # local REPL; needs `ollama serve` running
make eval MODEL=qwen3:8b  # eval suite; 15-20 min, this is how a model gets picked
make eval-compare MODELS=a,b,c   # scoreboard across models; ~15 min each

Eval progress goes to STDERR, so `eval --json > out.json` still shows live
progress. eval-compare saves each model to evals/results/ as it finishes and
reuses those on a re-run, so a stopped comparison resumes; --fresh redoes all.

Unbuilt targets exit 1 with the phase they land in; that's expected, not a
broken setup. Everything runs through `uv run` — don't call bare `python`
(system python is 3.9).