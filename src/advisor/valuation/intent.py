"""Weighting win-now against future according to the team's stated intent.

Applied **last**, on purpose. Everything upstream keeps `win_now` and `future`
separate, so the two unweighted numbers are always available for the model to
talk about. This step exists only when something has to be collapsed into a
single comparable figure.

The weights are the difference between "sell your 29-year-old back" and "buy
one". They are user intent, never inferred — see `league_format.TEAM_INTENTS`.
"""

from __future__ import annotations

from dataclasses import dataclass

from advisor.context import LeagueContext
from advisor.league_format import BALANCED, CONTEND, DEFAULT_TEAM_INTENT, REBUILD
from advisor.valuation.base import PlayerValue, RosterValue

# (win_now weight, future weight). Contend and rebuild are mirror images so that
# neither stance is quietly favoured by the arithmetic.
INTENT_WEIGHTS: dict[str, tuple[float, float]] = {
    CONTEND: (0.80, 0.20),
    BALANCED: (0.50, 0.50),
    REBUILD: (0.20, 0.80),
}


@dataclass(frozen=True)
class WeightedValue:
    """One comparable number, with the inputs it came from kept alongside."""

    win_now: float
    future: float
    intent: str
    win_now_weight: float
    future_weight: float

    @property
    def combined(self) -> float:
        return round(
            self.win_now * self.win_now_weight + self.future * self.future_weight, 2
        )

    def explain(self) -> str:
        return (
            f"{self.combined:.1f} = {self.win_now_weight:.0%} x win_now "
            f"{self.win_now:.1f} + {self.future_weight:.0%} x future "
            f"{self.future:.1f}  (intent={self.intent})"
        )


# Single-year formats take win-now at full weight. Not merely because `future`
# is 0 there — applying the intent split anyway would rescale win_now and make
# the same redraft player look four times more valuable to a contender than to
# a rebuilder, which is nonsense when the roster resets in January.
SINGLE_YEAR_WEIGHTS = (1.0, 0.0)


def weights_for(intent: str) -> tuple[float, float]:
    return INTENT_WEIGHTS.get(intent, INTENT_WEIGHTS[DEFAULT_TEAM_INTENT])


def weights_for_context(ctx: LeagueContext) -> tuple[float, float]:
    """Intent is a multi-year concept; single-year formats ignore it."""
    if not ctx.is_multi_year:
        return SINGLE_YEAR_WEIGHTS
    return weights_for(ctx.team_intent)


def weigh(win_now: float, future: float, ctx: LeagueContext) -> WeightedValue:
    """Collapse the two figures using this team's intent."""
    win_now_weight, future_weight = weights_for_context(ctx)
    return WeightedValue(
        win_now=win_now,
        future=future,
        intent=ctx.team_intent,
        win_now_weight=win_now_weight,
        future_weight=future_weight,
    )


def combined_value(value: PlayerValue | RosterValue, ctx: LeagueContext) -> float:
    """Single comparable number for a player or roster under this team's stance."""
    return weigh(value.win_now, value.future, ctx).combined
