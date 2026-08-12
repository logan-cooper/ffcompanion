"""League rules -> fantasy points, and simple projections. Phase 3.

Format-agnostic by design: "how many points did this stat line produce under
these rules" has the same answer in redraft and dynasty. Age and format logic
belong in `valuation/` (Phase 3b), never here.
"""

from advisor.scoring.engine import ScoreBreakdown, score_stat_line, score_stat_line_detailed
from advisor.scoring.projections import (
    Projection,
    Scarcity,
    points_above_replacement,
    positional_scarcity,
    project_player,
    starter_demand,
)

__all__ = [
    "Projection",
    "Scarcity",
    "ScoreBreakdown",
    "points_above_replacement",
    "positional_scarcity",
    "project_player",
    "score_stat_line",
    "score_stat_line_detailed",
    "starter_demand",
]
