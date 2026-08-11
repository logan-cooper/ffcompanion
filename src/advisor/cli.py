"""Command line entry point.

Phase 0 shipped `--version`; Phase 1 adds `ingest` and `status`. The chat REPL
arrives in Phase 5.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from advisor import __version__

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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "ingest":
        return _cmd_ingest(args)
    if args.command == "status":
        return _cmd_status(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
