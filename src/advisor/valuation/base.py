"""The interface both valuation strategies implement.

`PlayerValue` carries **both** figures in every format. In redraft `future` is
simply zero — that keeps one code path instead of two and lets the tool layer
stay identical across formats. What changes between formats is what the numbers
*mean*, not their shape.

Keeping win-now and future separate all the way to the response is deliberate:
it lets the model articulate the tradeoff ("you lose 4 points a week now to gain
three years of a 22-year-old") instead of collapsing it into one score nobody
can argue with.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from advisor.context import LeagueContext

# Units: season points above replacement. win_now is rest-of-season; future is
# discounted points above replacement across later seasons.


@dataclass(frozen=True)
class PlayerValue:
    """What a player is worth, split into the two things that can conflict."""

    player_id: str
    name: str = ""
    position: str | None = None
    age: float | None = None
    win_now: float = 0.0
    future: float = 0.0
    points_per_game: float = 0.0
    replacement_points_per_game: float = 0.0
    detail: dict[str, float] = field(default_factory=dict)

    @property
    def total(self) -> float:
        """Unweighted sum. Prefer `intent.combined_value` for comparisons —
        this ignores whether the team is contending or rebuilding."""
        return round(self.win_now + self.future, 2)

    def explain(self) -> str:
        label = self.name or self.player_id
        age = f", age {self.age:.1f}" if self.age is not None else ""
        return (
            f"{label} ({self.position}{age}): "
            f"win_now {self.win_now:+.1f}, future {self.future:+.1f}"
        )


@dataclass(frozen=True)
class PickValue:
    """A future draft pick. Picks are real assets in dynasty, worthless in redraft."""

    season: int
    round: int
    win_now: float = 0.0
    future: float = 0.0

    @property
    def label(self) -> str:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(self.round, "th")
        return f"{self.season}-{self.round}{suffix}"

    @property
    def total(self) -> float:
        return round(self.win_now + self.future, 2)


@dataclass(frozen=True)
class RosterValue:
    """A whole roster, with the per-player detail retained."""

    roster_id: int
    players: tuple[PlayerValue, ...] = ()
    picks: tuple[PickValue, ...] = ()

    @property
    def win_now(self) -> float:
        return round(
            sum(p.win_now for p in self.players) + sum(k.win_now for k in self.picks), 2
        )

    @property
    def future(self) -> float:
        return round(
            sum(p.future for p in self.players) + sum(k.future for k in self.picks), 2
        )

    @property
    def average_age(self) -> float | None:
        ages = [p.age for p in self.players if p.age is not None]
        return round(sum(ages) / len(ages), 1) if ages else None


@runtime_checkable
class Valuation(Protocol):
    """Redraft and dynasty implement this identically-shaped interface."""

    name: str

    def player_value(self, player_id: str, ctx: LeagueContext) -> PlayerValue: ...

    def pick_value(self, season: int, round_: int, ctx: LeagueContext) -> PickValue: ...

    def roster_value(self, roster_id: int, ctx: LeagueContext) -> RosterValue: ...
