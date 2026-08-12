"""Shared plumbing for the tool layer.

Every tool returns a JSON-serialisable dict wrapped in the same envelope, so the
model always knows which frame it is reasoning in: league format, team intent,
where in the season we are, and how stale the data is. Echoing that back on
every response is cheaper and far more reliable than hoping it remembers the
system prompt twelve turns later.

Tools never branch on format. They ask `get_valuation(ctx)` and report whatever
it returns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from advisor.context import LeagueContext
from advisor.db import query
from advisor.league_format import MULTI_YEAR_FORMATS
from advisor.scoring.engine import score_stat_line

# Roughly 1500 tokens. Tools truncate rather than blow the model's context, and
# say so when they do.
MAX_RESPONSE_CHARS = 6000
CHARS_PER_TOKEN = 4


def error(message: str, detail: str = "") -> dict:
    """Tools never return an empty success — no data is an explicit error."""
    return {"error": message, "detail": detail}


def data_as_of(ctx: LeagueContext) -> str | None:
    rows = query(
        "SELECT MAX(fetched_at) AS at FROM ingest_log WHERE season = ?",
        [ctx.stats_season or ctx.season],
    )
    at = rows[0]["at"] if rows else None
    return at.strftime("%Y-%m-%d %H:%M UTC") if at else None


def envelope(ctx: LeagueContext) -> dict:
    """The context block attached to every tool response."""
    return {
        "league": {
            "league_id": ctx.league_id,
            "name": ctx.name,
            "format": ctx.format,
            "superflex": ctx.superflex,
            "team_intent": ctx.team_intent,
            "teams": ctx.total_rosters,
            **_intent_caveat(ctx),
        },
        "as_of": {
            "season": ctx.season,
            "current_week": ctx.current_week,
            "season_started": ctx.season_started,
            "season_complete": ctx.season_complete,
            # In the offseason every number below is last season's, aged
            # forward. Saying so stops the model presenting it as this year's.
            "stats_from_season": ctx.stats_season,
            "basis": _basis(ctx),
            "data_as_of": data_as_of(ctx),
            **_win_now_caveat(ctx),
        },
    }


def _intent_caveat(ctx: LeagueContext) -> dict:
    """Explain an intent that is carried but does not apply.

    Contending and rebuilding are multi-year ideas: in a single-year format the
    intent weights are (1.0, 0.0), so intent changes no number this tool
    returns. The envelope still carries it — every response carries the same
    keys on purpose — but unlabelled, a model reads `team_intent: rebuild` and
    reasons from it, which produced a redraft answer arguing from "since your
    team is rebuilding". Same principle as the win_now caveat below: a value
    that cannot mean what it appears to mean has to say so.
    """
    if ctx.format in MULTI_YEAR_FORMATS:
        return {}
    return {
        "team_intent_does_not_apply": (
            f"{ctx.format} is a single-year format, so contending and rebuilding "
            "are the same thing: only rest-of-season production matters. Do not "
            "reason from team_intent here."
        )
    }


def _win_now_caveat(ctx: LeagueContext) -> dict:
    """Explain a zero that would otherwise read as a verdict.

    With no games left, win_now is zero for every player by arithmetic — there
    is nothing left to win. Unlabelled, a model seeing `win_now: 0.0` beside an
    elite receiver will reasonably conclude he is worthless.
    """
    if ctx.games_remaining > 0:
        return {}
    return {
        "win_now_is_zero_for_everyone": (
            f"the {ctx.season} season is over, so no player has rest-of-season "
            "value. This is not a judgement on any player — compare on `future` "
            "and on season production instead."
        )
    }


def _basis(ctx: LeagueContext) -> str:
    if ctx.is_offseason:
        return (
            f"{ctx.season} has not started; figures are {ctx.stats_season} "
            "production aged forward"
        )
    if ctx.current_week <= 4:
        return (
            f"week {ctx.current_week} of {ctx.season}; small sample, figures are "
            f"anchored to {ctx.season - 1}"
        )
    if not ctx.season_complete:
        return f"through week {ctx.current_week} of {ctx.season}"
    return f"{ctx.season} regular season complete"


def truncate(items: list, limit: int, label: str = "items") -> tuple[list, str | None]:
    """Cap a list, returning a note when anything was dropped."""
    if len(items) <= limit:
        return items, None
    return items[:limit], f"showing {limit} of {len(items)} {label}"


def fits(payload: dict) -> bool:
    return len(json.dumps(payload, default=str)) <= MAX_RESPONSE_CHARS


def fit_to_budget(payload: dict, trim_keys: list[str]) -> dict:
    """Shrink `payload` until it fits, exhausting `trim_keys` **in order**.

    Guessing per-list caps does not work — a 30-player dynasty roster overruns a
    budget a 13-player redraft roster sits comfortably inside. So measure, then
    trim.

    Order matters more than size: trimming whichever list happens to be longest
    would drop starters from a deep roster, and a lineup question cannot be
    answered without them. Pass the least decision-relevant keys first and they
    are emptied before anything important is touched.
    """
    dropped: dict[str, int] = {}

    for key in trim_keys:
        while not fits(payload):
            items = payload.get(key)
            if not isinstance(items, list) or len(items) <= 1:
                break
            keep = max(1, int(len(items) * 0.75))
            dropped[key] = dropped.get(key, 0) + (len(items) - keep)
            payload[key] = items[:keep]
        if fits(payload):
            break

    if dropped:
        notes = payload.setdefault("truncated", [])
        if not isinstance(notes, list):
            notes = payload["truncated"] = [notes]
        notes += [
            f"{key}: dropped {count} to fit the response budget"
            for key, count in dropped.items()
        ]
    return payload


def estimated_tokens(payload: dict) -> int:
    return len(json.dumps(payload, default=str)) // CHARS_PER_TOKEN


# --------------------------------------------------------------- player index

@dataclass
class PlayerLine:
    """One player's scored season under a specific league's rules."""

    player_id: str
    sleeper_id: str | None
    name: str
    position: str | None
    team: str | None
    age: float | None
    games: int
    total_points: float
    points_per_game: float
    last3_points_per_game: float
    weekly: list[tuple[int, float]]

    @property
    def trend(self) -> float:
        """Last three games minus season average. Positive means heating up."""
        return round(self.last3_points_per_game - self.points_per_game, 2)


_index_cache: dict[tuple[str, int], dict[str, PlayerLine]] = {}


def clear_index_cache() -> None:
    _index_cache.clear()


def player_index(ctx: LeagueContext) -> dict[str, PlayerLine]:
    """Every player's season, scored once under this league's rules.

    Built in a single pass and memoised: scoring one player at a time would mean
    thousands of queries for a tool like `get_league_rosters`.
    """
    season = ctx.stats_season or ctx.season
    key = (ctx.league_id, season)
    if key in _index_cache:
        return _index_cache[key]

    rows = query(
        """
        SELECT s.*, p.full_name, p.sleeper_id, p.age, p.position AS profile_position
        FROM player_week_stats s
        LEFT JOIN players p ON p.player_id = s.player_id AND p.season = s.season
        WHERE s.season = ? AND s.season_type = 'REG'
        ORDER BY s.player_id, s.week
        """,
        [season],
    )

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["player_id"], []).append(row)

    index: dict[str, PlayerLine] = {}
    for player_id, weeks in grouped.items():
        first = weeks[0]
        position = first["profile_position"] or first["position"]
        scored = [
            (w["week"], score_stat_line(w, ctx.scoring_settings, position=position))
            for w in weeks
        ]
        points = [p for _, p in scored]
        last3 = points[-3:]
        index[player_id] = PlayerLine(
            player_id=player_id,
            sleeper_id=first["sleeper_id"],
            name=first["full_name"] or player_id,
            position=position,
            team=weeks[-1]["team"],
            age=first["age"],
            games=len(points),
            total_points=round(sum(points), 2),
            points_per_game=round(sum(points) / len(points), 2) if points else 0.0,
            last3_points_per_game=round(sum(last3) / len(last3), 2) if last3 else 0.0,
            weekly=[(w, round(p, 2)) for w, p in scored],
        )

    _index_cache[key] = index
    return index


def by_sleeper_id(ctx: LeagueContext) -> dict[str, PlayerLine]:
    return {
        line.sleeper_id: line
        for line in player_index(ctx).values()
        if line.sleeper_id
    }


def roster_slots(ctx: LeagueContext, roster_id: int) -> dict[str, list[str]]:
    """Sleeper ids by slot for one roster: starters, bench, taxi, reserve."""
    rows = query(
        "SELECT players, starters, taxi, reserve FROM league_rosters "
        "WHERE league_id = ? AND roster_id = ?",
        [ctx.league_id, roster_id],
    )
    if not rows:
        return {}

    row = rows[0]
    players = [str(p) for p in json.loads(row["players"] or "[]")]
    starters = [str(p) for p in json.loads(row["starters"] or "[]") if p]
    taxi = [str(p) for p in json.loads(row["taxi"] or "[]")]
    reserve = [str(p) for p in json.loads(row["reserve"] or "[]")]
    special = set(starters) | set(taxi) | set(reserve)

    return {
        "starters": starters,
        "bench": [p for p in players if p not in special],
        "taxi": taxi,
        "reserve": reserve,
    }


def league_owners(ctx: LeagueContext) -> dict[int, str]:
    rows = query(
        """
        SELECT r.roster_id,
               COALESCE(u.team_name, u.display_name, '(orphan)') AS team
        FROM league_rosters r
        LEFT JOIN league_users u
          ON u.league_id = r.league_id AND u.user_id = r.owner_id
        WHERE r.league_id = ?
        """,
        [ctx.league_id],
    )
    return {r["roster_id"]: r["team"] for r in rows}


def owner_of(ctx: LeagueContext) -> dict[str, int]:
    """Sleeper player_id -> roster_id for everyone rostered in this league."""
    rows = query(
        "SELECT roster_id, players, taxi, reserve FROM league_rosters WHERE league_id = ?",
        [ctx.league_id],
    )
    owned: dict[str, int] = {}
    for row in rows:
        for key in ("players", "taxi", "reserve"):
            for pid in json.loads(row[key] or "[]"):
                owned[str(pid)] = row["roster_id"]
    return owned


def as_value_dict(value: Any) -> dict:
    """Compact win-now / future pair from a PlayerValue."""
    return {"win_now": value.win_now, "future": value.future}
