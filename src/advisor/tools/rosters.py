"""get_my_roster and get_league_rosters.

`get_my_roster` is deep on one team; `get_league_rosters` is deliberately shallow
across all of them. Returning full stat lines for every team would blow the
response budget and bury the thing that actually matters at a glance: who is
strong where, and who is old.
"""

from __future__ import annotations

from advisor.context import LeagueContext
from advisor.db import query
from advisor.tools.base import (
    envelope,
    error,
    fit_to_budget,
    league_owners,
    player_index,
    roster_slots,
    truncate,
)
from advisor.valuation import get_valuation

MAX_PLAYERS_PER_SLOT = 20
AGE_POSITIONS = ("RB", "WR")

# Sleeper writes "0" into a starting slot nobody is filling.
EMPTY_STARTING_SLOT = "0"


def player_entry(line, value, ctx: LeagueContext) -> dict:
    """One player's line, as every caller reports it.

    Public because the web panels render the same players beside the answers
    that cite them. Two shapes here would mean the sidebar and the chat quoting
    different numbers for the same player, which costs more trust than it saves
    effort.
    """
    entry = {
        "player_id": line.player_id,
        "name": line.name,
        "position": line.position,
        "team": line.team,
        # `games` travels with every total: it is what separates "declined"
        # from "was hurt", and without it the rate stats are misreadable.
        "games": line.games,
        "season_points": line.total_points,
        "points_per_game": line.points_per_game,
        "last3_points_per_game": line.last3_points_per_game,
        "trend_vs_season": line.trend,
    }
    if line.age is not None:
        entry["age"] = round(line.age, 1)
    if value is not None:
        entry["win_now"] = value.win_now
        entry["future"] = value.future
    return entry


def get_my_roster(ctx: LeagueContext, roster_id: int | None = None) -> dict:
    """One team in depth: starters, bench, taxi, reserve, and owned picks."""
    roster_id = roster_id if roster_id is not None else ctx.roster_id
    if roster_id is None:
        return {**envelope(ctx), **error("no roster_id", "pass roster_id")}

    slots = roster_slots(ctx, roster_id)
    if not slots:
        return {
            **envelope(ctx),
            **error(
                f"roster {roster_id} not found",
                f"league {ctx.league_id} has no such roster",
            ),
        }

    index = player_index(ctx)
    by_sleeper = {line.sleeper_id: line for line in index.values() if line.sleeper_id}
    valuation = get_valuation(ctx)

    payload = {**envelope(ctx), "roster_id": roster_id,
               "team": league_owners(ctx).get(roster_id, "?")}

    unmatched: list[str] = []
    for slot, sleeper_ids in slots.items():
        entries = []
        for sleeper_id in sleeper_ids:
            line = by_sleeper.get(sleeper_id)
            if line is None:
                # "0" is Sleeper's empty starting slot, not a player. Counting
                # it inflated "N players had no stats" with a gap in the lineup.
                if sleeper_id != EMPTY_STARTING_SLOT:
                    unmatched.append(sleeper_id)
                continue
            value = valuation.player_value(line.player_id, ctx)
            entries.append(player_entry(line, value, ctx))
        entries.sort(key=lambda e: -(e.get("win_now", 0) + e.get("future", 0)))
        shown, note = truncate(entries, MAX_PLAYERS_PER_SLOT, slot)
        payload[slot] = shown
        if note:
            payload.setdefault("truncated", []).append(note)

    if unmatched:
        # Named, not just counted. The panel beside the answer shows these
        # players by name, and "you also have 2 players with no stats" next to a
        # sidebar reading "Tyrone Broden" is the disagreement this app cannot
        # afford. Sleeper knows everyone; nflverse only lists NFL rosters.
        from advisor.warehouse.refresh import identify

        known = identify(unmatched)
        payload["rostered_without_stats"] = [
            {
                "name": known.get(s, {}).get("full_name") or f"Sleeper player {s}",
                "position": known.get(s, {}).get("position"),
                "team": known.get(s, {}).get("team"),
                "status": known.get(s, {}).get("status"),
            }
            for s in unmatched
        ]
        payload["note"] = (
            f"{len(unmatched)} rostered players had no {ctx.stats_season} stats "
            "(rookies, practice squad, or out of the league); they are listed "
            "under rostered_without_stats"
        )

    picks = _owned_picks(ctx, roster_id, valuation)
    if picks is not None:
        payload["draft_picks"] = picks

    # Trim the deepest, least decision-relevant lists first. Starters are the
    # last thing to go — a lineup question cannot be answered without them.
    return fit_to_budget(payload, ["reserve", "taxi", "draft_picks", "bench", "starters"])


def _owned_picks(ctx: LeagueContext, roster_id: int, valuation) -> list | None:
    """Future picks, when the format has them at all."""
    rows = query(
        """
        SELECT season, round, original_roster_id, owner_roster_id
        FROM traded_picks WHERE league_id = ? AND season > ?
        ORDER BY season, round
        """,
        [ctx.league_id, ctx.season],
    )
    if not rows:
        return None

    traded_away = {
        (r["season"], r["round"])
        for r in rows
        if r["original_roster_id"] == roster_id and r["owner_roster_id"] != roster_id
    }
    acquired = [
        (r["season"], r["round"], r["original_roster_id"])
        for r in rows
        if r["owner_roster_id"] == roster_id and r["original_roster_id"] != roster_id
    ]

    picks = []
    for season, round_ in sorted({(r["season"], r["round"]) for r in rows}):
        if (season, round_) in traded_away:
            continue
        value = valuation.pick_value(season, round_, ctx)
        picks.append({"pick": value.label, "own": True, "future_value": value.future})
    for season, round_, origin in acquired:
        value = valuation.pick_value(season, round_, ctx)
        picks.append(
            {
                "pick": value.label,
                "via_roster_id": origin,
                "future_value": value.future,
            }
        )
    return picks


def get_league_rosters(ctx: LeagueContext) -> dict:
    """Every team, compact: positional strength and (dynasty) how old they are."""
    rows = query(
        "SELECT roster_id, wins, losses, ties FROM league_rosters "
        "WHERE league_id = ? ORDER BY roster_id",
        [ctx.league_id],
    )
    if not rows:
        return {
            **envelope(ctx),
            **error("league has no rosters", "run link-league first"),
        }

    index = player_index(ctx)
    by_sleeper = {line.sleeper_id: line for line in index.values() if line.sleeper_id}
    teams = league_owners(ctx)

    summaries = []
    for row in rows:
        roster_id = row["roster_id"]
        slots = roster_slots(ctx, roster_id)
        lines = [
            by_sleeper[s]
            for slot in ("starters", "bench", "taxi", "reserve")
            for s in slots.get(slot, [])
            if s in by_sleeper
        ]

        strength: dict[str, float] = {}
        ages: dict[str, list[float]] = {}
        for line in lines:
            if not line.position:
                continue
            strength[line.position] = round(
                strength.get(line.position, 0.0) + line.points_per_game, 1
            )
            if line.age is not None:
                ages.setdefault(line.position, []).append(line.age)

        summary = {
            "roster_id": roster_id,
            "team": teams.get(roster_id, "?"),
            "record": f"{row['wins'] or 0}-{row['losses'] or 0}",
            "players": len(lines),
            "points_per_game_by_position": dict(sorted(strength.items())),
        }

        # Average age at RB/WR is how you spot who is rebuilding and who is
        # pushing. Only meaningful where rosters carry over.
        if ctx.is_multi_year:
            skill_ages = [a for p in AGE_POSITIONS for a in ages.get(p, [])]
            if skill_ages:
                summary["avg_age_rb_wr"] = round(sum(skill_ages) / len(skill_ages), 1)

        summaries.append(summary)

    shown, note = truncate(summaries, 16, "teams")
    payload = {**envelope(ctx), "rosters": shown}
    if note:
        payload["truncated"] = note
    return payload
