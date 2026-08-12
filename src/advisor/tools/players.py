"""resolve_player — turn a name into candidate player_ids.

Name-to-id ambiguity is the app's single biggest failure mode, so this returns a
ranked *list* rather than a best guess, and says who owns each candidate. Two
players share a surname far more often than you would like ("Harrison",
"Williams", "Brown"), and picking silently is how the assistant ends up
confidently quoting the wrong man's stat line.
"""

from __future__ import annotations

from advisor.context import LeagueContext
from advisor.db import query
from advisor.tools.base import (
    envelope,
    error,
    league_owners,
    owner_of,
    player_index,
    truncate,
)

MAX_CANDIDATES = 6

# Suffixes and punctuation people leave out when typing a name.
_NOISE = (".", ",", "'", "-")
_SUFFIXES = (" jr", " sr", " ii", " iii", " iv", " v")


def _normalise(name: str) -> str:
    lowered = name.lower().strip()
    for character in _NOISE:
        lowered = lowered.replace(character, "")
    for suffix in _SUFFIXES:
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
    return " ".join(lowered.split())


def _rank(candidate: str, wanted: str) -> int:
    """Lower is better.

    Deliberately not position-biased: a bare surname must rank a first-name
    match and a last-name match equally, so that production breaks the tie.
    Ranking prefixes higher would surface a kicker named Harrison above Marvin
    Harrison Jr., which is the wrong-player failure mode this tool exists to
    prevent.
    """
    if candidate == wanted:
        return 0
    tokens = candidate.split()
    if wanted in tokens:
        return 1
    if candidate.startswith(wanted):
        return 2
    if any(token.startswith(wanted) for token in tokens):
        return 3
    return 4


def resolve_player(
    query_text: str,
    ctx: LeagueContext,
    *,
    limit: int = MAX_CANDIDATES,
) -> dict:
    """Find players matching `query_text`, with their current owner."""
    wanted = _normalise(query_text)
    if not wanted:
        return {**envelope(ctx), **error("empty query", "pass a player name")}

    season = ctx.stats_season or ctx.season
    rows = query(
        """
        SELECT player_id, full_name, position, team, age, sleeper_id
        FROM players
        WHERE season = ? AND full_name IS NOT NULL
          AND position IN ('QB','RB','WR','TE','K')
        """,
        [season],
    )

    matches = []
    for row in rows:
        candidate = _normalise(row["full_name"])
        tokens = candidate.split()
        if wanted in candidate or any(token.startswith(wanted) for token in tokens):
            matches.append((_rank(candidate, wanted), row))

    if not matches:
        return {
            **envelope(ctx),
            **error(
                f"no player matches {query_text!r}",
                "check spelling, or the player may not have played this season",
            ),
        }

    index = player_index(ctx)
    owners = owner_of(ctx)
    teams = league_owners(ctx)

    # Rank by match quality, then by production — "Josh Allen" should surface the
    # quarterback before the linebacker.
    matches.sort(
        key=lambda m: (m[0], -(index[m[1]["player_id"]].total_points
                               if m[1]["player_id"] in index else 0))
    )

    candidates = []
    for _, row in matches:
        line = index.get(row["player_id"])
        roster_id = owners.get(str(row["sleeper_id"])) if row["sleeper_id"] else None
        candidates.append(
            {
                "player_id": row["player_id"],
                "name": row["full_name"],
                "position": row["position"],
                "team": row["team"],
                "age": round(row["age"], 1) if row["age"] is not None else None,
                "games": line.games if line else 0,
                "points_per_game": line.points_per_game if line else 0.0,
                "owner": (
                    {"roster_id": roster_id, "team": teams.get(roster_id, "?")}
                    if roster_id
                    else "free agent"
                ),
            }
        )

    shown, note = truncate(candidates, limit, "candidates")
    payload = {**envelope(ctx), "query": query_text, "candidates": shown}
    if note:
        payload["truncated"] = note
    if len(shown) > 1:
        payload["ambiguous"] = (
            "more than one match — confirm which player before using a player_id"
        )
    return payload
