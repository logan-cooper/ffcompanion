"""Command line entry point.

Phase 0 ships only `--version`. The chat REPL arrives in Phase 5.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from advisor import __version__


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
