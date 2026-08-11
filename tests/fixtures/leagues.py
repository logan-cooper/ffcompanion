"""Raw Sleeper league objects used to test format detection offline.

DYNASTY_SUPERFLEX and SURVIVAL are trimmed copies of real leagues, so their
`settings.type` values are confirmed observations.

REDRAFT and KEEPER are hand-written. Sleeper documents type 0 and 1 as redraft
and keeper, but no live league was available to confirm them — the roadmap calls
for a written fixture in exactly this case rather than shipping an untested
detector. If you ever link a real redraft or keeper league, check its raw
`sleeper_type` against these before trusting them.
"""

from __future__ import annotations

STANDARD_POSITIONS = [
    "QB", "RB", "RB", "WR", "WR", "TE", "FLEX",
    "BN", "BN", "BN", "BN", "BN", "BN",
]

SUPERFLEX_POSITIONS = [
    "QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "FLEX", "SUPER_FLEX",
    "BN", "BN", "BN", "BN", "BN", "BN",
]

TWO_QB_POSITIONS = [
    "QB", "QB", "RB", "RB", "WR", "WR", "TE", "FLEX",
    "BN", "BN", "BN", "BN",
]

# ---------------------------------------------------------------- hand-written

REDRAFT = {
    "league_id": "fixture-redraft",
    "name": "Fixture Redraft",
    "season": "2025",
    "status": "complete",
    "total_rosters": 12,
    "previous_league_id": None,
    "roster_positions": STANDARD_POSITIONS,
    "scoring_settings": {"rec": 1.0, "pass_td": 4.0, "rush_yd": 0.1},
    "settings": {"type": 0, "taxi_slots": 0, "taxi_years": 0, "num_teams": 12},
}

KEEPER = {
    "league_id": "fixture-keeper",
    "name": "Fixture Keeper",
    "season": "2025",
    "status": "complete",
    "total_rosters": 12,
    "previous_league_id": None,
    "roster_positions": STANDARD_POSITIONS,
    "scoring_settings": {"rec": 0.5, "pass_td": 4.0},
    "settings": {"type": 1, "taxi_slots": 0, "max_keepers": 3, "num_teams": 12},
}

# A redraft league that isn't really one: `type` says 0 but it carries a taxi
# squad. Detection must refuse rather than believe the type field.
REDRAFT_CONTRADICTED_BY_TAXI = {
    **REDRAFT,
    "league_id": "fixture-contradiction-taxi",
    "settings": {**REDRAFT["settings"], "taxi_slots": 3},
}

REDRAFT_CONTRADICTED_BY_CONTINUATION = {
    **REDRAFT,
    "league_id": "fixture-contradiction-prev",
    "previous_league_id": "some-earlier-league",
}

UNRECOGNISED_TYPE = {
    **REDRAFT,
    "league_id": "fixture-future-type",
    "settings": {**REDRAFT["settings"], "type": 99},
}

MISSING_TYPE = {
    **REDRAFT,
    "league_id": "fixture-no-type",
    "settings": {"taxi_slots": 0},
}

TWO_QB_REDRAFT = {
    **REDRAFT,
    "league_id": "fixture-2qb",
    "roster_positions": TWO_QB_POSITIONS,
}

# ------------------------------------------------------ trimmed from real data

# Wolfpack Dynasty (10-team superflex dynasty, continues a 2024 league).
DYNASTY_SUPERFLEX = {
    "league_id": "1180641430628597760",
    "name": "Wolfpack Dynasty ",
    "season": "2025",
    "status": "complete",
    "total_rosters": 10,
    "previous_league_id": "1112479688682868736",
    "roster_positions": SUPERFLEX_POSITIONS,
    "scoring_settings": {"rec": 1.0, "pass_td": 4.0},
    "settings": {
        "type": 2,
        "taxi_slots": 3,
        "taxi_years": 2,
        "max_keepers": 1,
        "num_teams": 10,
    },
}

# 814 Survival — a survival league, not a redraft. No persistent rosters.
SURVIVAL = {
    "league_id": "1262131969181896704",
    "name": "814 survival ",
    "season": "2025",
    "status": "complete",
    "total_rosters": 16,
    "previous_league_id": None,
    "roster_positions": [
        "QB", "RB", "WR", "FLEX", "FLEX", "FLEX", "FLEX", "FLEX",
        "BN", "BN", "BN", "BN", "BN",
    ],
    "scoring_settings": {"rec": 1.0, "bonus_rec_te": 0.5},
    "settings": {
        "type": 3,
        "taxi_slots": 0,
        "max_keepers": 1,
        "num_teams": 16,
    },
}
