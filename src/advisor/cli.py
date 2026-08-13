"""Command line entry point.

Phase 0 shipped `--version`; Phase 1 adds `ingest` and `status`. The chat REPL
arrives in Phase 5.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence

from advisor import __version__
from advisor.league_format import TEAM_INTENTS

# nflverse weekly stats begin in 1999. The upper bound is loose on purpose so a
# new season can be ingested the moment it opens.
EARLIEST_SEASON = 1999
LATEST_SEASON = 2100


def season_list(raw: str) -> list[int]:
    """Parse `2023` or `2023,2024,2025` into a list of seasons.

    Used as an argparse `type`, so raising ArgumentTypeError gives the user a
    usage error rather than a traceback.
    """
    seasons = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            season = int(part)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a season: {part!r}") from None
        if not EARLIEST_SEASON <= season <= LATEST_SEASON:
            raise argparse.ArgumentTypeError(
                f"season {season} outside {EARLIEST_SEASON}-{LATEST_SEASON}"
            )
        seasons.append(season)

    if not seasons:
        raise argparse.ArgumentTypeError("no seasons given")
    # Preserve order but drop repeats, so 2024,2024 doesn't ingest twice.
    return list(dict.fromkeys(seasons))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="advisor",
        description="Fantasy football advisor.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"advisor {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    ingest = subparsers.add_parser(
        "ingest", help="Load a season of nflverse stats into the local warehouse."
    )
    ingest.add_argument(
        "--season",
        dest="seasons",
        type=season_list,
        action="extend",
        required=True,
        help="Season to load. Comma-separated and/or repeatable: "
        "--season 2023,2024,2025",
    )
    ingest.add_argument(
        "--week",
        type=int,
        action="append",
        dest="weeks",
        help="Limit weekly tables to this week. Repeatable. Default: all weeks.",
    )
    ingest.add_argument(
        "--refresh",
        action="store_true",
        help="Re-download instead of reusing the Parquet cache.",
    )

    status = subparsers.add_parser(
        "status", help="Show what has been ingested and when."
    )
    status.add_argument("--season", type=int, default=None)

    link = subparsers.add_parser(
        "link-league", help="Pull a Sleeper user's leagues into the database."
    )
    link.add_argument("--username", required=True)
    link.add_argument("--season", type=int, required=True)
    link.add_argument("--league-id", default=None, help="Limit to one league.")
    link.add_argument(
        "--refresh", action="store_true", help="Re-download the Sleeper player dump."
    )

    verify = subparsers.add_parser(
        "verify-scoring",
        help="Check scored points against what Sleeper actually recorded.",
    )
    verify.add_argument("--season", type=int, default=2025)

    chat = subparsers.add_parser(
        "chat", help="Conversational REPL. Runs entirely on your machine."
    )
    chat.add_argument("--league-id", default=None)
    chat.add_argument("--roster-id", type=int, default=None)
    chat.add_argument("--week", type=int, default=None)
    chat.add_argument("--model", default=None, help="Override the Ollama model tag.")
    chat.add_argument(
        "--verbose",
        action="store_true",
        help="Print every tool call and result. Your main debugging tool.",
    )

    evals = subparsers.add_parser(
        "eval", help="Run the eval suite. Use it to choose a model."
    )
    evals.add_argument("--model", default=None, help="Model tag, or omit for configured.")
    evals.add_argument("--league-id", default=None)
    evals.add_argument("--week", type=int, default=None)
    evals.add_argument("--case", default=None, help="Run one case by id.")
    evals.add_argument("--json", action="store_true", help="Machine-readable output.")
    evals.add_argument(
        "--thinking",
        action="store_true",
        help="Let a reasoning model think first. Off by default — it measured "
        "worse AND slower here; use this to re-check that on a new model.",
    )

    compare = subparsers.add_parser(
        "eval-compare",
        help="Run the suite against several models and print a scoreboard.",
    )
    compare.add_argument(
        "--models",
        default="qwen3:8b,llama3.1:8b,hermes3:8b",
        help="Comma-separated model tags.",
    )
    compare.add_argument("--league-id", default=None)
    compare.add_argument("--week", type=int, default=None)
    compare.add_argument(
        "--out",
        default="evals/results",
        help="Where finished per-model runs are saved, so a stopped comparison resumes.",
    )
    compare.add_argument(
        "--fresh",
        action="store_true",
        help="Re-run every model instead of reusing saved runs.",
    )

    demo = subparsers.add_parser(
        "tools-demo",
        help="Run all six tools under both a dynasty and a redraft context.",
    )
    demo.add_argument("--league-id", default=None)
    demo.add_argument("--roster-id", type=int, default=None)

    intent = subparsers.add_parser(
        "set-intent",
        help="Record how a team is playing the season (contend/rebuild/balanced).",
    )
    intent.add_argument("--league-id", required=True)
    intent.add_argument("--roster-id", type=int, required=True)
    intent.add_argument("--intent", required=True, choices=TEAM_INTENTS)

    return parser


def _cmd_ingest(args: argparse.Namespace) -> int:
    from advisor.warehouse import ingest_season

    for season in args.seasons:
        summary = ingest_season(season, weeks=args.weeks, refresh=args.refresh)
        print(summary)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from advisor.db import query

    clause = " WHERE season = ?" if args.season else ""
    params = [args.season] if args.season else []
    rows = query(
        "SELECT source, season, COUNT(*) AS entries, "
        "       MIN(week) AS first_week, MAX(week) AS last_week, "
        "       SUM(row_count) AS total_rows, MAX(fetched_at) AS fetched_at "
        f"FROM ingest_log{clause} "
        "GROUP BY source, season ORDER BY season DESC, source",
        params,
    )
    if not rows:
        print("Nothing ingested yet. Run: make ingest SEASON=2025")
        return 0

    print(f"{'source':<20}{'season':>7}{'weeks':>12}{'rows':>10}  fetched_at")
    for r in rows:
        weeks = (
            "-"
            if r["first_week"] is None
            else f"{r['first_week']}-{r['last_week']}"
        )
        print(
            f"{r['source']:<20}{r['season']:>7}{weeks:>12}"
            f"{r['total_rows']:>10,}  {r['fetched_at']:%Y-%m-%d %H:%M} UTC"
        )
    return 0


POSITION_ORDER = ("QB", "RB", "WR", "TE", "K", "DEF")


def _roster_by_position(player_ids, pool) -> str:
    """Render a roster as position-grouped, wrapped name lists."""
    import textwrap

    grouped: dict[str, list[str]] = {}
    for pid in player_ids or []:
        player = pool.get(str(pid)) or {}
        position = player.get("position") or "?"
        name = player.get("full_name") or str(pid)
        grouped.setdefault(position, []).append(name)

    if not grouped:
        return "      (no rostered players)"

    ordered = [p for p in POSITION_ORDER if p in grouped]
    ordered += sorted(set(grouped) - set(POSITION_ORDER))

    lines = []
    for position in ordered:
        names = ", ".join(sorted(grouped[position]))
        wrapped = textwrap.wrap(names, width=88) or [""]
        lines.append(f"      {position:<4} {wrapped[0]}")
        lines += [f"           {line}" for line in wrapped[1:]]
    return "\n".join(lines)


def _cmd_link_league(args: argparse.Namespace) -> int:
    import json

    from advisor.db import query
    from advisor.sources import sleeper
    from advisor.warehouse.leagues import link_leagues

    user_id, summaries = link_leagues(
        args.username,
        args.season,
        league_id=args.league_id,
        refresh=args.refresh,
    )
    pool = sleeper.get_all_players()
    print(f"Sleeper user {args.username} -> {user_id}\n")

    for s in summaries:
        d = s.detection
        print("=" * 92)
        print(f"{s.name}   (league {s.league_id})   season {s.season}")
        print(
            f"  format: {d.format:<9} superflex: {'yes' if d.superflex else 'no':<4}"
            f" taxi: {'yes' if d.has_taxi else 'no':<4}"
            f" continuation: {'yes' if d.is_continuation else 'no'}"
        )
        print(f"  detected via: {d.source}")
        if d.needs_user_confirmation:
            print("  !! format unresolved — the app must ask before giving advice")
        print(
            f"  {s.rosters} rosters | {s.users} users | {s.rostered_players} rostered"
            f" | {s.available_players:,} free agents | {s.traded_picks} traded picks"
        )

        rosters = query(
            """
            SELECT r.roster_id, r.players, r.starters, r.wins, r.losses,
                   COALESCE(u.team_name, u.display_name, '(orphan)') AS team,
                   r.owner_id
            FROM league_rosters r
            LEFT JOIN league_users u
              ON u.league_id = r.league_id AND u.user_id = r.owner_id
            WHERE r.league_id = ? ORDER BY r.roster_id
            """,
            [s.league_id],
        )

        mine = [r for r in rosters if r["owner_id"] == user_id]
        others = [r for r in rosters if r["owner_id"] != user_id]

        for label, group in (("YOUR ROSTER", mine), ("OPPONENTS", others)):
            if not group:
                continue
            print(f"\n  {label}")
            for r in group:
                players = json.loads(r["players"] or "[]")
                starters = json.loads(r["starters"] or "[]")
                print(
                    f"    roster {r['roster_id']:<3} {r['team']:<28}"
                    f" {len(players):>3} players, {len(starters):>2} starters"
                    f"  ({r['wins'] or 0}-{r['losses'] or 0})"
                )
                print(_roster_by_position(players, pool))

        if s.traded_picks:
            picks = query(
                "SELECT season, round, original_roster_id, owner_roster_id "
                "FROM traded_picks WHERE league_id = ? "
                "ORDER BY season, round, original_roster_id LIMIT 10",
                [s.league_id],
            )
            print(f"\n  TRADED PICKS (first 10 of {s.traded_picks})")
            for p in picks:
                print(
                    f"    {p['season']} round {p['round']}"
                    f"  originally roster {p['original_roster_id']}"
                    f" -> now roster {p['owner_roster_id']}"
                )
        print()
    return 0


def _cmd_verify_scoring(args: argparse.Namespace) -> int:
    from advisor.scoring.validate import validate_all

    results = validate_all(args.season)
    print(f"Scoring vs points Sleeper recorded, {args.season}:\n")
    for result in results:
        print(result)

    compared = sum(r.compared for r in results)
    exact = sum(r.exact for r in results)
    print(f"\nTOTAL {exact}/{compared} = {exact / max(compared, 1):.2%} exact")
    return 0


CHAT_HELP = """commands:
  /leagues       list linked leagues
  /league        show current league details
  /league N|id   switch leagues (clears the conversation)
  /week N        set the current week (changes what "recent" means)
  /intent X      contend | rebuild | balanced  (dynasty only)
  /format        show the detected league format
  /roster N      answer as a different roster
  /verbose       toggle tool-call tracing
  /reset         clear the conversation
  /help /quit
"""


def _league_line(row: dict, index: int, *, current: bool) -> str:
    """One league as a selectable row."""
    roster = f"roster {row['roster_id']}" if row["roster_id"] is not None else "—"
    return (
        f"  {'*' if current else ' '} {index}. {row['name'][:24]:<26}"
        f"{row['format']:<10}{row['season']:<6}{roster}"
    )


def _find_league(rest: str, rows: list[dict]) -> dict | None:
    """Resolve a /league argument: a 1-based index from /leagues, or a league id."""
    rest = rest.strip()
    if rest.isdigit():
        index = int(rest)
        if 1 <= index <= len(rows):
            return rows[index - 1]
        # Fall through: a numeric league id is also all digits, so a number
        # outside the list range is still worth matching by id.
    return next((r for r in rows if r["league_id"] == rest), None)


def _owned_roster(league_id: str) -> int | None:
    """Which roster in this league belongs to the configured user.

    Sleeper knows: league_rosters.owner_id joins league_users.user_id, whose
    display_name is the username. Without this a fresh install has no
    team_intent rows, so roster_id came back None and "how does my roster look?"
    was answered with "please provide your roster_id" — a question the user
    cannot sensibly answer and the loop is supposed to bind for them.
    """
    from advisor.context import list_leagues

    for row in list_leagues():
        if row["league_id"] == league_id:
            return row["owned_roster_id"]
    return None


def _pick_league(league_id: str | None) -> tuple[str, int | None]:
    """Resolve which league and roster to open.

    A lookup into `list_leagues()`, never its own query — the ordering that
    decides the default has to be the same one the UI renders, or the dropdown's
    first entry and the default answer disagree.
    """
    from advisor.context import list_leagues

    leagues = list_leagues()
    if not leagues:
        raise LookupError(
            "No leagues linked. Run: make link-league USERNAME=<you> SEASON=2025"
        )

    if league_id:
        match = next((r for r in leagues if r["league_id"] == league_id), None)
        if match is None:
            # Checked here rather than left to load_context. Unvalidated, a bad
            # id reached the web layer's response generator and killed the SSE
            # stream after a 200 had already been sent — which a browser can
            # only render as an answer stopping mid-sentence.
            linked = ", ".join(f"{r['league_id']} ({r['name']})" for r in leagues[:5])
            raise LookupError(f"league {league_id!r} is not linked. Linked: {linked}")
        return match["league_id"], match["roster_id"]

    return leagues[0]["league_id"], leagues[0]["roster_id"]


def _cmd_chat(args: argparse.Namespace) -> int:
    import dataclasses

    from advisor.agent import Turn, new_backend, run_turn
    from advisor.agent.backend import BackendError
    from advisor.context import list_leagues, load_context
    from advisor.league_format import TEAM_INTENTS

    try:
        league_id, default_roster = _pick_league(args.league_id)
    except LookupError as exc:
        print(exc)
        return 1

    ctx = load_context(
        league_id,
        roster_id=args.roster_id if args.roster_id is not None else default_roster,
        current_week=args.week,
    )

    try:
        backend = new_backend() if args.model is None else _backend_for(args.model)
    except BackendError as exc:
        # The runtime not being started is the normal first-run failure; it
        # deserves the fix, not a traceback.
        print(f"\n{exc}\n")
        return 1

    verbose = args.verbose
    messages: list[dict] = []

    print(f"\n{ctx.name} — {ctx.format}", end="")
    if ctx.is_multi_year:
        print(f", intent={ctx.team_intent}", end="")
    print(f", roster {ctx.roster_id}")
    print(f"{backend.name} — running locally, no API cost. /help for commands.\n")

    while True:
        try:
            raw = input(f"{ctx.name[:18]}/{ctx.roster_id}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not raw:
            continue

        if raw.startswith("/"):
            command, _, rest = raw.partition(" ")
            rest = rest.strip()

            if command in ("/quit", "/exit"):
                return 0
            if command == "/help":
                print(CHAT_HELP)
            elif command == "/reset":
                messages.clear()
                print("conversation cleared")
            elif command == "/verbose":
                verbose = not verbose
                print(f"verbose {'on' if verbose else 'off'}")
            elif command == "/format":
                print(f"{ctx.format} (detected: {ctx.needs_format_confirmation=})")
            elif command == "/leagues":
                rows = list_leagues()
                for index, row in enumerate(rows, 1):
                    print(
                        _league_line(
                            row, index, current=row["league_id"] == ctx.league_id
                        )
                    )
                print("\n  /league N to switch")
            elif command == "/league":
                if not rest:
                    print(
                        f"{ctx.name} | {ctx.total_rosters} teams | superflex="
                        f"{ctx.superflex} | week {ctx.current_week} of {ctx.season} | "
                        f"stats from {ctx.stats_season}"
                    )
                else:
                    target = _find_league(rest, list_leagues())
                    if target is None:
                        print(f"no league matching {rest!r}. /leagues for the list.")
                    else:
                        ctx = load_context(
                            target["league_id"],
                            roster_id=target["roster_id"],
                            # A hand-set week only carries within a season:
                            # week 14 of 2024 and of 2025 are not the same
                            # question.
                            current_week=(
                                ctx.current_week
                                if target["season"] == ctx.season
                                else None
                            ),
                        )
                        # Not optional. Leaving league A's history in front of
                        # league B's system prompt is the same failure the web
                        # layer's pinning fixes.
                        messages.clear()
                        print(
                            f"now in {ctx.name} ({ctx.format}), roster "
                            f"{ctx.roster_id} — conversation cleared"
                        )
            elif command == "/week":
                if rest.isdigit():
                    ctx = dataclasses.replace(ctx, current_week=int(rest))
                    print(f"week set to {ctx.current_week}")
                else:
                    print(f"current week: {ctx.current_week}. Usage: /week 14")
            elif command == "/roster":
                if rest.isdigit():
                    ctx = load_context(
                        ctx.league_id,
                        roster_id=int(rest),
                        current_week=ctx.current_week,
                    )
                    print(f"answering as roster {ctx.roster_id}")
                else:
                    print(f"current roster: {ctx.roster_id}. Usage: /roster 8")
            elif command == "/intent":
                if rest in TEAM_INTENTS:
                    from advisor.warehouse.leagues import set_team_intent

                    set_team_intent(ctx.league_id, ctx.roster_id, rest)
                    ctx = dataclasses.replace(ctx, team_intent=rest)
                    print(f"intent set to {rest} (saved)")
                else:
                    print(f"intent is {ctx.team_intent}. Usage: /intent {TEAM_INTENTS}")
            else:
                print(f"unknown command {command}. /help for the list.")
            continue

        messages.append({"role": "user", "content": raw})
        turn: Turn = run_turn(
            backend, ctx, messages, verbose=verbose, on_event=lambda s: print(s)
        )
        print(f"\n{turn.text}\n")
        print(f"  {turn.summary()} tools: {turn.tools_used or 'none'}")
        if turn.hit_iteration_cap:
            print("  (hit the tool-call cap)")
        print()


def _cmd_eval(args: argparse.Namespace) -> int:
    from advisor.agent.backend import BackendError
    from advisor.agent.ollama import EVAL_SEED
    from advisor.config import get_settings
    from advisor.context import load_context
    from advisor.evals import as_json, load_cases, report, run_suite

    try:
        league_id, default_roster = _pick_league(args.league_id)
    except LookupError as exc:
        print(exc)
        return 1

    ctx = load_context(league_id, roster_id=default_roster, current_week=args.week)

    # Evals pin the seed so a rerun is reproducible; chat deliberately does not.
    try:
        backend = _backend_for(
            args.model or get_settings().model,
            seed=EVAL_SEED,
            think=True if args.thinking else None,
        )
    except BackendError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    cases = load_cases()
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"no case named {args.case!r}")
            return 1

    # Progress goes to stderr, results to stdout. That split is what makes
    # `eval --json > results.json` still watchable — sending progress to stdout
    # meant redirecting the results also swallowed every sign of life, so a
    # 20-minute run looked identical to a hung one.
    print(f"\n{backend.name} — {len(cases)} cases against {ctx.name}", file=sys.stderr)
    print("running locally; this costs time, not money.\n", file=sys.stderr, flush=True)

    # flush=True matters more than it looks: a local suite takes minutes, and
    # without it Python buffers every progress line until the run ends, so a
    # working eval is indistinguishable from a hung one.
    done = 0
    started = time.monotonic()

    def progress(result) -> None:
        nonlocal done
        done += 1
        elapsed = time.monotonic() - started
        eta = (elapsed / done) * (len(cases) - done)
        print(
            f"  [{done}/{len(cases)}] {'pass' if result.passed else 'FAIL'}  "
            f"{result.id:<22} {result.seconds:>5.1f}s   ~{eta / 60:.1f}m left",
            file=sys.stderr,
            flush=True,
        )

    results = run_suite(backend, ctx, cases, on_result=progress)

    print(as_json(results, backend.name) if args.json else report(results, backend.name))
    return 0 if all(r.passed for r in results) else 1


def _cmd_eval_compare(args: argparse.Namespace) -> int:
    """Run the same suite against several models. This is how one gets chosen."""
    from pathlib import Path

    from advisor.agent.backend import BackendError
    from advisor.agent.ollama import EVAL_SEED
    from advisor.context import load_context
    from advisor.evals import load_cases, per_case_matrix, run_suite, scoreboard
    from advisor.evals.runner import load_run, save_run

    try:
        league_id, default_roster = _pick_league(args.league_id)
    except LookupError as exc:
        print(exc)
        return 1

    ctx = load_context(league_id, roster_id=default_roster, current_week=args.week)
    cases = load_cases()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    results_dir = Path(args.out)

    total_minutes = len(models) * len(cases) * 1.2
    # Progress on stderr, the scoreboard on stdout — this run is long enough
    # that piping the comparison to a file is the normal way to use it.
    print(f"\n{len(models)} models x {len(cases)} cases against {ctx.name}", file=sys.stderr)
    print(
        f"rough estimate {total_minutes:.0f} min. Costs time, not money.\n",
        file=sys.stderr,
        flush=True,
    )

    runs: dict[str, list] = {}
    for model in models:
        cached = None if args.fresh else load_run(results_dir, model, len(cases))
        if cached is not None:
            runs[model] = cached
            print(
                f"--- {model} --- reusing finished run "
                f"({sum(r.passed for r in cached)}/{len(cached)}); --fresh to redo",
                file=sys.stderr,
                flush=True,
            )
            continue

        try:
            backend = _backend_for(model, seed=EVAL_SEED)
        except BackendError as exc:
            print(f"skipping {model}: {exc}\n", file=sys.stderr, flush=True)
            continue

        print(f"--- {model} ---", file=sys.stderr, flush=True)
        started = time.monotonic()
        done = 0

        def progress(result) -> None:
            nonlocal done
            done += 1
            elapsed = time.monotonic() - started
            eta = (elapsed / done) * (len(cases) - done)
            print(
                f"  [{done}/{len(cases)}] {'pass' if result.passed else 'FAIL'}  "
                f"{result.id:<22} {result.seconds:>5.1f}s   ~{eta / 60:.1f}m left",
                file=sys.stderr,
                flush=True,
            )

        runs[model] = run_suite(backend, ctx, cases, on_result=progress)
        # Persist per model, not at the end: closing the laptop during model
        # three should not cost models one and two.
        saved = save_run(results_dir, model, runs[model])
        print(f"  saved {saved}\n", file=sys.stderr, flush=True)

    if not runs:
        print("no models could be evaluated", file=sys.stderr)
        return 1

    print(scoreboard(runs))
    print(per_case_matrix(runs))
    return 0


def _backend_for(model: str, *, seed: int | None = None, think: bool | None = None):
    from advisor.agent.ollama import OllamaBackend

    backend = OllamaBackend(model=model, seed=seed, think=think)
    backend.health()
    return backend


def _cmd_tools_demo(args: argparse.Namespace) -> int:
    from advisor.tools.demo import run_demo

    return run_demo(args.league_id, args.roster_id)


def _cmd_set_intent(args: argparse.Namespace) -> int:
    from advisor.warehouse.leagues import set_team_intent

    set_team_intent(args.league_id, args.roster_id, args.intent)
    print(
        f"roster {args.roster_id} in league {args.league_id} -> intent={args.intent}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "ingest":
        return _cmd_ingest(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "link-league":
        return _cmd_link_league(args)
    if args.command == "verify-scoring":
        return _cmd_verify_scoring(args)
    if args.command == "chat":
        return _cmd_chat(args)
    if args.command == "eval":
        return _cmd_eval(args)
    if args.command == "eval-compare":
        return _cmd_eval_compare(args)
    if args.command == "tools-demo":
        return _cmd_tools_demo(args)
    if args.command == "set-intent":
        return _cmd_set_intent(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
