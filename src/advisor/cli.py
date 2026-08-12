"""Command line entry point.

Phase 0 shipped `--version`; Phase 1 adds `ingest` and `status`. The chat REPL
arrives in Phase 5.
"""

from __future__ import annotations

import argparse
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
  /week N     set the current week (changes what "recent" means)
  /intent X   contend | rebuild | balanced  (dynasty only)
  /format     show the detected league format
  /league     show league details
  /roster N   answer as a different roster
  /verbose    toggle tool-call tracing
  /reset      clear the conversation
  /help /quit
"""


def _pick_league(league_id: str | None) -> tuple[str, int | None]:
    """Resolve which league and roster to open, preferring one with an intent set."""
    from advisor.db import query

    if league_id:
        rows = query(
            "SELECT roster_id FROM team_intent WHERE league_id = ? LIMIT 1", [league_id]
        )
        return league_id, rows[0]["roster_id"] if rows else None

    rows = query(
        """
        SELECT l.league_id, t.roster_id
        FROM leagues l
        LEFT JOIN team_intent t ON t.league_id = l.league_id
        ORDER BY t.roster_id IS NULL, l.name
        LIMIT 1
        """
    )
    if not rows:
        raise LookupError(
            "No leagues linked. Run: make link-league USERNAME=<you> SEASON=2025"
        )
    return rows[0]["league_id"], rows[0]["roster_id"]


def _cmd_chat(args: argparse.Namespace) -> int:
    import dataclasses

    from advisor.agent import Turn, new_backend, run_turn
    from advisor.agent.backend import BackendError
    from advisor.context import load_context
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
            raw = input("> ").strip()
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
            elif command == "/league":
                print(
                    f"{ctx.name} | {ctx.total_rosters} teams | superflex="
                    f"{ctx.superflex} | week {ctx.current_week} of {ctx.season} | "
                    f"stats from {ctx.stats_season}"
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
                        league_id, roster_id=int(rest), current_week=ctx.current_week
                    )
                    print(f"answering as roster {ctx.roster_id}")
                else:
                    print(f"current roster: {ctx.roster_id}. Usage: /roster 8")
            elif command == "/intent":
                if rest in TEAM_INTENTS:
                    from advisor.warehouse.leagues import set_team_intent

                    set_team_intent(league_id, ctx.roster_id, rest)
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
    from advisor.agent import new_backend
    from advisor.agent.backend import BackendError
    from advisor.context import load_context
    from advisor.evals import as_json, load_cases, report, run_suite

    try:
        league_id, default_roster = _pick_league(args.league_id)
    except LookupError as exc:
        print(exc)
        return 1

    ctx = load_context(league_id, roster_id=default_roster, current_week=args.week)

    try:
        backend = new_backend() if args.model is None else _backend_for(args.model)
    except BackendError as exc:
        print(f"\n{exc}\n")
        return 1

    cases = load_cases()
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"no case named {args.case!r}")
            return 1

    if not args.json:
        print(f"\n{backend.name} — {len(cases)} cases against {ctx.name}")
        print("running locally; this costs time, not money.\n", flush=True)

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
            flush=True,
        )

    results = run_suite(
        backend, ctx, cases, on_result=(lambda _: None) if args.json else progress
    )

    print(as_json(results, backend.name) if args.json else report(results, backend.name))
    return 0 if all(r.passed for r in results) else 1


def _backend_for(model: str):
    from advisor.agent.ollama import OllamaBackend

    backend = OllamaBackend(model=model)
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
    if args.command == "tools-demo":
        return _cmd_tools_demo(args)
    if args.command == "set-intent":
        return _cmd_set_intent(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
