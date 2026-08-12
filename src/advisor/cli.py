"""Command line entry point.

Phase 0 shipped `--version`; Phase 1 adds `ingest` and `status`. The chat REPL
arrives in Phase 5.
"""

from __future__ import annotations

import argparse
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
    if args.command == "set-intent":
        return _cmd_set_intent(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
