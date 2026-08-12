"""Dynasty valuation: this season plus the ones after it.

Win-now is identical to redraft — the same rest-of-season points above
replacement — so `RedraftValuation` is composed rather than reimplemented. What
dynasty adds is `future`: discounted production across a multi-year horizon,
bent by a positional aging curve, plus draft picks as real assets.

This is why an aging productive back and a younger lesser one can be worth the
same here and nowhere else.
"""

from __future__ import annotations

import json

from advisor.context import LeagueContext
from advisor.db import query
from advisor.valuation.aging import projected_multiplier
from advisor.valuation.base import PickValue, PlayerValue, RosterValue
from advisor.valuation.picks import PickSlot, pick_par_value
from advisor.valuation.redraft import (
    RedraftValuation,
    player_snapshot,
    replacement_level,
    roster_player_ids,
)

# How many future seasons count. Three is the roadmap's window: far enough that
# age matters, near enough that the projection isn't fiction.
HORIZON_YEARS = 3

# Per-year discount. **The main dial between contend-leaning and rebuild-leaning
# advice** — raise it and the future matters more. Deliberately a named constant
# so tuning it is a one-line, reviewable change.
DISCOUNT_RATE = 0.75

GAMES_PER_SEASON = 17

# A superflex league can start a second quarterback, so QB production is scarcer
# and holds value better. This is the single largest format-level swing in
# dynasty valuation.
SUPERFLEX_QB_MULTIPLIER = 1.35


class DynastyValuation:
    """Win-now plus discounted, age-adjusted future value."""

    name = "dynasty"

    def __init__(self) -> None:
        self._redraft = RedraftValuation()

    def player_value(self, player_id: str, ctx: LeagueContext) -> PlayerValue:
        base = self._redraft.player_value(player_id, ctx)
        snapshot = player_snapshot(player_id, ctx)

        position = snapshot["position"]
        age = snapshot["age"]
        per_game = snapshot["points_per_game"]
        replacement = replacement_level(ctx, position)

        future = 0.0
        per_year: dict[str, float] = {}
        for years_ahead in range(1, HORIZON_YEARS + 1):
            multiplier = projected_multiplier(position, age, years_ahead)
            season_per_game = per_game * multiplier
            # Floored per year, not on the sum: a player who ages below
            # replacement in year 3 is simply off the roster by then, worth
            # zero rather than a liability. Without this, an aging star scores
            # hugely negative and the model would recommend paying to dump him.
            above_replacement = (
                max(0.0, season_per_game - replacement) * GAMES_PER_SEASON
            )
            discounted = above_replacement * (DISCOUNT_RATE**years_ahead)
            per_year[f"year_+{years_ahead}"] = round(discounted, 2)
            future += discounted

        if ctx.superflex and (position or "").upper() == "QB":
            future *= SUPERFLEX_QB_MULTIPLIER
            per_year["superflex_qb_multiplier"] = SUPERFLEX_QB_MULTIPLIER

        return PlayerValue(
            player_id=base.player_id,
            name=base.name,
            position=position,
            age=age,
            win_now=base.win_now,
            future=round(future, 2),
            points_per_game=per_game,
            replacement_points_per_game=replacement,
            detail={**base.detail, **per_year},
        )

    def pick_value(
        self,
        season: int,
        round_: int,
        ctx: LeagueContext,
        *,
        slot: PickSlot = PickSlot.MID,
    ) -> PickValue:
        """Future picks are assets. All of their value is future value."""
        seasons_away = max(0, season - ctx.season)
        value = pick_par_value(
            round_,
            seasons_away=seasons_away,
            slot=slot,
            superflex=ctx.superflex,
        )
        return PickValue(season=season, round=round_, win_now=0.0, future=value)

    def roster_value(self, roster_id: int, ctx: LeagueContext) -> RosterValue:
        players = tuple(
            self.player_value(pid, ctx) for pid in roster_player_ids(roster_id, ctx)
        )
        return RosterValue(
            roster_id=roster_id,
            players=players,
            picks=tuple(self._owned_picks(roster_id, ctx)),
        )

    def _owned_picks(self, roster_id: int, ctx: LeagueContext) -> list[PickValue]:
        """Future picks this roster currently holds.

        Only traded picks are recorded by Sleeper; a pick that never moved is
        still owned by its original team but has no row. Both are counted.
        """
        traded = query(
            """
            SELECT season, round, original_roster_id, owner_roster_id
            FROM traded_picks WHERE league_id = ? AND season > ?
            """,
            [ctx.league_id, ctx.season],
        )

        traded_away = {
            (r["season"], r["round"])
            for r in traded
            if r["original_roster_id"] == roster_id and r["owner_roster_id"] != roster_id
        }
        acquired = [
            (r["season"], r["round"])
            for r in traded
            if r["owner_roster_id"] == roster_id
        ]

        seasons = sorted({r["season"] for r in traded}) or [ctx.season + 1]
        own = [
            (season, round_)
            for season in seasons
            for round_ in (1, 2, 3)
            if (season, round_) not in traded_away
        ]

        return [self.pick_value(s, r, ctx) for s, r in own + acquired]
