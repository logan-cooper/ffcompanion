"""TOOLS (schemas sent to the API) and REGISTRY (name -> callable).

The `description` fields are prompt engineering, not documentation. They are the
only instructions the model gets about *when* to reach for a tool, so they say
what the tool is for and what it will not do, in the order the model needs it.

Every callable takes `league_id` and builds its own `LeagueContext`, so the
agent loop never has to thread context through.
"""

from __future__ import annotations

from typing import Any, Callable

from advisor.context import load_context
from advisor.tools.compare import MAX_PLAYERS, MAX_WEEKS, compare_players
from advisor.tools.players import resolve_player
from advisor.tools.rosters import get_league_rosters, get_my_roster
from advisor.tools.trade import evaluate_trade
from advisor.tools.waivers import MAX_LIMIT, get_available_players

LEAGUE_ID_SCHEMA = {
    "type": "string",
    "description": "The league to answer for. Required on every tool.",
}


def _resolve_player(league_id: str, query: str, **_: Any) -> dict:
    return resolve_player(query, load_context(league_id))


def _get_my_roster(league_id: str, roster_id: int, **_: Any) -> dict:
    return get_my_roster(load_context(league_id, roster_id=roster_id), roster_id)


def _get_league_rosters(league_id: str, **_: Any) -> dict:
    return get_league_rosters(load_context(league_id))


def _compare_players(
    league_id: str, player_ids: list[str], weeks: int = MAX_WEEKS, **_: Any
) -> dict:
    return compare_players(load_context(league_id), player_ids, weeks=weeks)


def _get_available_players(
    league_id: str, position: str | None = None, limit: int = MAX_LIMIT, **_: Any
) -> dict:
    return get_available_players(load_context(league_id), position, limit=limit)


def _evaluate_trade(
    league_id: str,
    my_roster_id: int,
    i_give: list[str],
    i_get: list[str],
    their_roster_id: int | None = None,
    **_: Any,
) -> dict:
    ctx = load_context(league_id, roster_id=my_roster_id)
    return evaluate_trade(ctx, my_roster_id, their_roster_id, i_give, i_get)


REGISTRY: dict[str, Callable[..., dict]] = {
    "resolve_player": _resolve_player,
    "get_my_roster": _get_my_roster,
    "get_league_rosters": _get_league_rosters,
    "compare_players": _compare_players,
    "get_available_players": _get_available_players,
    "evaluate_trade": _evaluate_trade,
}


TOOLS: list[dict] = [
    {
        "name": "resolve_player",
        "description": (
            "Turn a player's name into candidate player_ids. CALL THIS FIRST for "
            "any player the user names, before any other tool that takes a "
            "player_id — every other tool needs an id, and guessing one is the "
            "most common way to end up quoting the wrong player's stats.\n\n"
            "Returns a ranked list, not a single answer, because surnames repeat. "
            "If more than one candidate comes back and the choice changes your "
            "advice, ask the user which they mean rather than picking. Each "
            "candidate includes who currently rosters them in this league, so "
            "you can tell a free agent from someone's starter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "league_id": LEAGUE_ID_SCHEMA,
                "query": {
                    "type": "string",
                    "description": (
                        "Name as the user wrote it. Partial and last-name-only "
                        "queries work; suffixes and punctuation are ignored."
                    ),
                },
            },
            "required": ["league_id", "query"],
        },
    },
    {
        "name": "get_my_roster",
        "description": (
            "One team in depth: starters, bench, taxi and IR, each with games "
            "played, season points, points per game, last-3-game form, and the "
            "trend between them. In formats where rosters carry over you also get "
            "each player's age and their win-now and future values, plus the "
            "draft picks the team owns.\n\n"
            "Use this before any start/sit or trade question about the user's own "
            "team. Read `games` alongside any total — a low season score with few "
            "games played means injury, not decline."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "league_id": LEAGUE_ID_SCHEMA,
                "roster_id": {
                    "type": "integer",
                    "description": "Which team. The user's own unless asked otherwise.",
                },
            },
            "required": ["league_id", "roster_id"],
        },
    },
    {
        "name": "get_league_rosters",
        "description": (
            "Every team in the league at a glance: record, how many players they "
            "have, and points per game by position. In formats where rosters "
            "carry over it also reports each team's average age at RB and WR, "
            "which is how you spot who is rebuilding and who is pushing to win "
            "now.\n\n"
            "Use it to find a trade partner — a team deep at a position you need "
            "and thin where you are strong. Deliberately shallow: for one team's "
            "detail call get_my_roster with their roster_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"league_id": LEAGUE_ID_SCHEMA},
            "required": ["league_id"],
        },
    },
    {
        "name": "compare_players",
        "description": (
            f"Compare up to {MAX_PLAYERS} players side by side: weekly points for "
            f"the last {MAX_WEEKS} games, season and recent averages, snap share, "
            "target share, red-zone volume, current owner, upcoming opponent and "
            "how that defense treats the position, plus win-now and future "
            "value.\n\n"
            "This is the tool for start/sit questions and for the 'is he better "
            "than' half of a trade discussion. Usage numbers (snaps, targets) "
            "often tell you more about the next month than points do, because "
            "they show role rather than outcomes. Requires player_ids from "
            "resolve_player."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "league_id": LEAGUE_ID_SCHEMA,
                "player_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"1-{MAX_PLAYERS} ids from resolve_player.",
                    "maxItems": MAX_PLAYERS,
                },
                "weeks": {
                    "type": "integer",
                    "description": f"Recent weeks to show, 1-{MAX_WEEKS}.",
                },
            },
            "required": ["league_id", "player_ids"],
        },
    },
    {
        "name": "get_available_players",
        "description": (
            "Free agents in this league, ranked by their last three games, with "
            "how many Sleeper managers added them in the past day. The pool is "
            "derived per league, so these really are unrostered here.\n\n"
            "Use it for waiver and streaming questions. Ranked on recent form "
            "rather than season totals, because the wire is about who is useful "
            "now. A high add count with weak production is hype; strong "
            "production with a low count is the pickup nobody has noticed yet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "league_id": LEAGUE_ID_SCHEMA,
                "position": {
                    "type": "string",
                    "enum": ["QB", "RB", "WR", "TE", "K"],
                    "description": "Filter to one position. Omit for all.",
                },
                "limit": {
                    "type": "integer",
                    "description": f"How many to return, up to {MAX_LIMIT}.",
                },
            },
            "required": ["league_id"],
        },
    },
    {
        "name": "evaluate_trade",
        "description": (
            "Price a proposed trade. Returns the win-now and future point swing "
            "for both sides, scarcity-adjusted against replacement level at each "
            "position, and how the user's startable depth changes.\n\n"
            "IT RETURNS NUMBERS AND NO VERDICT — deciding is your job. Weigh the "
            "two figures using the league format and team intent in the response: "
            "a contending team should discount future value, a rebuilding one "
            "should discount win-now. In a single-year format future is always "
            "zero and only win-now matters.\n\n"
            "Both sides accept player_ids and draft picks in the same list. Write "
            "picks as \"2027-1st\" or \"2027-2nd\"."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "league_id": LEAGUE_ID_SCHEMA,
                "my_roster_id": {
                    "type": "integer",
                    "description": "The user's roster_id.",
                },
                "their_roster_id": {
                    "type": "integer",
                    "description": "The other team's roster_id, if known.",
                },
                "i_give": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        'What the user sends: player_ids and/or picks like "2027-1st".'
                    ),
                },
                "i_get": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "What the user receives, same format.",
                },
            },
            "required": ["league_id", "my_roster_id", "i_give", "i_get"],
        },
    },
]


def tool_names() -> list[str]:
    return [tool["name"] for tool in TOOLS]


def validate_registry() -> None:
    """Every advertised tool must be callable, and vice versa."""
    advertised = set(tool_names())
    implemented = set(REGISTRY)
    if advertised != implemented:
        raise RuntimeError(
            f"registry mismatch: advertised-only={advertised - implemented}, "
            f"implemented-only={implemented - advertised}"
        )
