"""evaluate_trade — what each side gains and loses, in numbers.

**Returns no verdict.** The tool reports win-now and future deltas for both
sides and how the starting lineup changes; deciding whether that is a good trade
is the model's job, and it needs the tradeoff visible rather than pre-collapsed
into a score.

`i_give` and `i_get` accept player_ids and pick references interchangeably
("2027-1st"), so a dynasty proposal involving picks works without a second tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from advisor.context import LeagueContext
from advisor.tools.base import (
    envelope,
    error,
    league_owners,
    player_index,
    roster_slots,
)
from advisor.valuation import get_valuation
from advisor.valuation.intent import weigh

MAX_ASSETS_PER_SIDE = 6

# "2027-1st", "2027 1st", "2027-1", "2027 round 1"
PICK_PATTERN = re.compile(
    r"^\s*(?P<season>20\d{2})\s*[-\s]\s*(?:round\s*)?(?P<round>\d)(?:st|nd|rd|th)?\s*$",
    re.IGNORECASE,
)


@dataclass
class Asset:
    label: str
    win_now: float
    future: float
    kind: str
    position: str | None = None


def parse_pick(reference: str) -> tuple[int, int] | None:
    """Parse a pick reference into (season, round). None if it isn't one."""
    match = PICK_PATTERN.match(str(reference))
    if not match:
        return None
    return int(match.group("season")), int(match.group("round"))


def _resolve_asset(reference: str, ctx: LeagueContext, valuation, index) -> Asset | None:
    pick = parse_pick(reference)
    if pick:
        season, round_ = pick
        value = valuation.pick_value(season, round_, ctx)
        return Asset(
            label=value.label,
            win_now=value.win_now,
            future=value.future,
            kind="pick",
        )

    line = index.get(reference)
    if line is None:
        return None
    value = valuation.player_value(reference, ctx)
    return Asset(
        label=line.name,
        win_now=value.win_now,
        future=value.future,
        kind="player",
        position=line.position,
    )


def _side(assets: list[Asset]) -> dict:
    return {
        "assets": [
            {
                "label": a.label,
                "kind": a.kind,
                "position": a.position,
                "win_now": a.win_now,
                "future": a.future,
            }
            for a in assets
        ],
        "win_now_total": round(sum(a.win_now for a in assets), 2),
        "future_total": round(sum(a.future for a in assets), 2),
    }


def _starter_depth(ctx: LeagueContext, roster_id: int, index, out_ids, in_ids) -> dict:
    """Startable players by position before and after the trade.

    'Startable' means above replacement — a body that has to start is not depth.
    """
    slots = roster_slots(ctx, roster_id)
    if not slots:
        return {}

    by_sleeper = {line.sleeper_id: line for line in index.values() if line.sleeper_id}
    current = [
        by_sleeper[s]
        for slot in ("starters", "bench", "taxi", "reserve")
        for s in slots.get(slot, [])
        if s in by_sleeper
    ]

    valuation = get_valuation(ctx)

    def count(lines) -> dict[str, int]:
        counts: dict[str, int] = {}
        for line in lines:
            if not line.position:
                continue
            value = valuation.player_value(line.player_id, ctx)
            if value.win_now > 0:
                counts[line.position] = counts.get(line.position, 0) + 1
        return dict(sorted(counts.items()))

    after = [line for line in current if line.player_id not in out_ids]
    after += [index[pid] for pid in in_ids if pid in index]

    return {"before": count(current), "after": count(after)}


def evaluate_trade(
    ctx: LeagueContext,
    my_roster_id: int,
    their_roster_id: int | None,
    i_give: list[str],
    i_get: list[str],
) -> dict:
    """Point deltas for both sides. No recommendation."""
    if not i_give and not i_get:
        return {
            **envelope(ctx),
            **error("empty trade", "pass at least one asset on one side"),
        }

    give_refs = list(i_give)[:MAX_ASSETS_PER_SIDE]
    get_refs = list(i_get)[:MAX_ASSETS_PER_SIDE]

    index = player_index(ctx)
    valuation = get_valuation(ctx)

    given, gotten, unresolved = [], [], []
    for reference in give_refs:
        asset = _resolve_asset(reference, ctx, valuation, index)
        given.append(asset) if asset else unresolved.append(reference)
    for reference in get_refs:
        asset = _resolve_asset(reference, ctx, valuation, index)
        gotten.append(asset) if asset else unresolved.append(reference)

    if unresolved and not (given or gotten):
        return {
            **envelope(ctx),
            **error(
                "could not resolve any asset",
                f"unknown: {unresolved}. Call resolve_player for names, "
                'or use a pick reference like "2027-1st"',
            ),
        }

    my_side, their_side = _side(given), _side(gotten)
    win_now_delta = round(their_side["win_now_total"] - my_side["win_now_total"], 2)
    future_delta = round(their_side["future_total"] - my_side["future_total"], 2)

    weighted = weigh(win_now_delta, future_delta, ctx)
    teams = league_owners(ctx)

    out_ids = {a.label for a in given}
    payload = {
        **envelope(ctx),
        "my_roster": {
            "roster_id": my_roster_id,
            "team": teams.get(my_roster_id, "?"),
            "i_give": my_side,
            "i_get": their_side,
            # Positive means this side gains. Both figures are reported
            # unweighted; `intent_weighted` is one way to read them, not the
            # answer.
            "win_now_delta": win_now_delta,
            "future_delta": future_delta,
            "intent_weighted_delta": weighted.combined,
            "weighting": weighted.explain(),
        },
        "their_roster": {
            "roster_id": their_roster_id,
            "team": teams.get(their_roster_id, "?") if their_roster_id else None,
            # The mirror image: what they give up is what you receive.
            "win_now_delta": -win_now_delta,
            "future_delta": -future_delta,
        },
        "no_verdict": (
            "numbers only — weigh win-now against future using the league "
            "format and team intent above"
        ),
    }

    give_player_ids = {r for r in give_refs if r in index}
    get_player_ids = [r for r in get_refs if r in index]
    depth = _starter_depth(
        ctx, my_roster_id, index, give_player_ids, get_player_ids
    )
    if depth:
        payload["my_roster"]["startable_by_position"] = depth

    if unresolved:
        payload["unresolved"] = unresolved
    return payload
