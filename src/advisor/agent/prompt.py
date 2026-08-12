"""The system prompt, assembled per league.

**Written for a small local model, and that changes the style.** The roadmap's
prompt was drafted for a frontier model: long, prose-heavy, full of nuance. A
7-8B model follows short, concrete, imperative instructions far more reliably
than long explanatory ones, and every token here competes with tool schemas and
tool results for a smaller context window.

So the rules below are terse and ordered by how badly breaking them hurts:
inventing a statistic is the worst failure this app can have, so it goes first.
"""

from __future__ import annotations

import json

from advisor.context import LeagueContext
from advisor.league_format import DYNASTY, KEEPER, SURVIVAL, UNKNOWN

MULTI_YEAR = (DYNASTY, KEEPER)


def _scoring_summary(ctx: LeagueContext) -> str:
    """The handful of scoring rules that actually change advice."""
    settings = ctx.scoring_settings
    notable = []
    for key, label in (
        ("rec", "{v} per reception"),
        ("pass_td", "{v} per passing TD"),
        ("bonus_rec_te", "+{v} per TE reception"),
        ("rec_fd", "{v} per receiving first down"),
    ):
        if key in settings:
            notable.append(label.format(v=settings[key]))
    return "; ".join(notable) or "standard"


def _format_block(ctx: LeagueContext) -> str:
    """The rules that differ by league format. One block, no branching prose."""
    if ctx.format in MULTI_YEAR:
        return (
            f"FORMAT: {ctx.format}. Rosters carry over. Player age and draft picks\n"
            f"are real assets. The user's stated intent is {ctx.team_intent}.\n"
            "Every player has a win_now number and a future number. Use both.\n"
            "Say which one you are prioritising and why. A contending team should\n"
            "discount future value; a rebuilding team should discount win-now."
        )
    if ctx.format == SURVIVAL:
        return (
            "FORMAT: survival. This league has no persistent rosters, so trade and\n"
            "dynasty logic does not apply. Say so plainly if asked about trades."
        )
    if ctx.format == UNKNOWN:
        return (
            "FORMAT: unknown. Ask the user whether this is redraft or dynasty\n"
            "BEFORE giving any trade advice. Do not guess."
        )
    return (
        "FORMAT: redraft. Rosters reset after this season. Ignore player age and\n"
        "future value entirely — only rest-of-season production matters.\n"
        "A 27-year-old and a 22-year-old producing the same are worth the same."
    )


def _timing_block(ctx: LeagueContext) -> str:
    """Where in the year we are, and how much to trust the numbers."""
    if ctx.is_offseason:
        return (
            f"TIMING: the {ctx.season} season has not started. Every number below\n"
            f"is {ctx.stats_season} production aged forward one year. Say so rather\n"
            "than presenting it as this season's."
        )
    if ctx.season_complete:
        return (
            f"TIMING: the {ctx.season} season is over. win_now is 0 for everyone\n"
            "because no games remain — that is arithmetic, NOT a judgement on any\n"
            "player. Compare on future value and season production instead."
        )
    if ctx.current_week <= 4:
        return (
            f"TIMING: week {ctx.current_week} of {ctx.season}. Small sample. These\n"
            f"numbers are mostly anchored to {ctx.season - 1}, so they carry real\n"
            "uncertainty. Say that rather than stating a week-2 read with\n"
            "December confidence."
        )
    return f"TIMING: through week {ctx.current_week} of {ctx.season}."


def build_system_prompt(ctx: LeagueContext) -> str:
    """Assemble the prompt for one league."""
    roster_positions = [p for p in ctx.roster_positions if p != "BN"]

    return f"""You are a fantasy football advisor for one specific league.

RULES, most important first:
1. NEVER state a statistic that did not come from a tool result. If a tool
   returns no data, say so plainly. Do not estimate or recall numbers.
2. Call resolve_player FIRST for any player the user names, before any tool
   that takes a player_id. Never invent a player_id.
3. Give a recommendation. State the tradeoff in one line, then commit to an
   answer. Advice that refuses to choose is useless.
4. Be concise. A few sentences beats a report.

LEAGUE: {ctx.name}
  {ctx.total_rosters} teams, superflex: {"yes" if ctx.superflex else "no"}
  Starters: {", ".join(roster_positions)}
  Scoring: {_scoring_summary(ctx)}
  The user's roster_id is {ctx.roster_id}.

{_format_block(ctx)}

{_timing_block(ctx)}

Every tool response repeats the league format, team intent, and where in the
season we are. Trust that block over anything you remember from earlier turns.
"""


def describe_tools_for_log(tools: list[dict]) -> str:
    """Compact tool listing for --verbose."""
    return json.dumps([t["name"] for t in tools])
