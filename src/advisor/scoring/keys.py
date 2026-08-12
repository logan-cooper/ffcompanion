"""The nflverse column -> Sleeper stat key mapping.

**This is the single most likely source of subtle wrong-number bugs in the app**,
so it lives in one dict, documented, rather than being spread through the
scoring code. A wrong entry here does not crash — it quietly produces plausible
points that are off by a few, which is the worst kind of bug for a tool whose
whole premise is that its numbers are traceable.

Sleeper's `scoring_settings` is a flat map of stat key -> points per unit.
Missing key means zero points, never an error.

Names that do NOT line up, i.e. the ones worth checking twice:

    nflverse                        Sleeper
    passing_interceptions           pass_int
    passing_first_downs             pass_fd
    passing_40                      pass_cmp_40p    (completions of 40+ yards)
    receiving_40                    rec_40p         (receptions of 40+)
    rushing_40                      rush_40p
    fumbles       (all)             fum
    fumbles_lost  (lost only)       fum_lost
    special_teams_tds               st_td
"""

from __future__ import annotations

# Sleeper stat key -> warehouse column. Straight per-unit multipliers.
STAT_KEY_COLUMNS: dict[str, str] = {
    # Passing
    "pass_yd": "passing_yards",
    "pass_td": "passing_tds",
    "pass_int": "interceptions",
    "pass_2pt": "passing_2pt_conversions",
    "pass_cmp": "completions",
    "pass_att": "attempts",
    "pass_cmp_40p": "passing_40",
    # Rushing
    "rush_yd": "rushing_yards",
    "rush_td": "rushing_tds",
    "rush_2pt": "rushing_2pt_conversions",
    "rush_att": "carries",
    "rush_40p": "rushing_40",
    # Receiving
    "rec": "receptions",
    "rec_yd": "receiving_yards",
    "rec_td": "receiving_tds",
    "rec_2pt": "receiving_2pt_conversions",
    "rec_tgt": "targets",
    "rec_40p": "receiving_40",
    # Turnovers. `fum` counts every fumble; `fum_lost` only the lost ones, and
    # a lost fumble scores under BOTH keys when a league sets both.
    "fum": "fumbles",
    "fum_lost": "fumbles_lost",
    # Return touchdowns. Only `st_td` belongs to the returning player —
    # `def_st_td` scores for the team defense unit and would double count here.
    "st_td": "special_teams_tds",
}

# First downs are scored specially, not as a plain multiplier.
#
# nflverse counts a touchdown as a first down; Sleeper does not. Left
# unadjusted this over-scores by one first down on every scoring play, which is
# small, plausible, and everywhere — measured against real recorded points it
# was 744 wrong player-weeks in a single league, and lifting the adjustment
# moved agreement from 66.5% to 97.9%.
#
# The warehouse deliberately stores nflverse's raw count; this convention
# belongs to the scoring platform, so it is applied here.
# key -> (first_down_column, touchdown_column)
FIRST_DOWN_KEYS: dict[str, tuple[str, str]] = {
    "pass_fd": ("passing_first_downs", "passing_tds"),
    "rush_fd": ("rushing_first_downs", "rushing_tds"),
    "rec_fd": ("receiving_first_downs", "receiving_tds"),
}

# Per-reception bonus applied only to a given position (tight end premium).
POSITION_RECEPTION_BONUSES: dict[str, str] = {
    "bonus_rec_te": "TE",
    "bonus_rec_rb": "RB",
    "bonus_rec_wr": "WR",
}

# Flat bonus awarded once when a column clears a threshold in a single game.
# (sleeper_key, column, threshold)
YARDAGE_MILESTONE_BONUSES: tuple[tuple[str, str, float], ...] = (
    ("bonus_pass_yd_300", "passing_yards", 300),
    ("bonus_pass_yd_400", "passing_yards", 400),
    ("bonus_rush_yd_100", "rushing_yards", 100),
    ("bonus_rush_yd_200", "rushing_yards", 200),
    ("bonus_rec_yd_100", "receiving_yards", 100),
    ("bonus_rec_yd_200", "receiving_yards", 200),
)

# Keys that belong to kickers, team defenses, or IDP. They are real scoring
# rules, but they never apply to an offensive skill player's stat line, so the
# engine ignores them instead of reporting them as unsupported.
NON_OFFENSIVE_PREFIXES = ("def_", "idp_", "pts_allow", "fgm", "fgmiss", "xpm", "xpmiss")

NON_OFFENSIVE_KEYS = frozenset(
    {
        "sack", "int", "ff", "fum_rec", "fum_rec_td", "safe", "blk_kick",
        "def_td", "tkl", "tkl_loss", "tkl_solo", "tkl_ast", "qb_hit",
        "pass_def", "blk_kick_ret_yd", "fgm_yds", "fga", "xpa",
        # Special-teams tackling stats belong to the defense, unlike st_td.
        "st_ff", "st_fum_rec",
    }
)

# Offensive keys we knowingly cannot compute from nflverse weekly stats.
# Surfaced rather than silently skipped.
KNOWN_UNSUPPORTED_KEYS = frozenset(
    {
        "pass_int_td",  # interceptions returned for a TD by the defense
        "pass_sack",  # sacks taken; stored upstream but not in the warehouse
        "fum_ret_yd",
        "rec_ret_yd",
    }
)


def is_offensive_key(key: str) -> bool:
    """False for kicker, team-defense, and IDP keys."""
    if key in NON_OFFENSIVE_KEYS:
        return False
    return not key.startswith(NON_OFFENSIVE_PREFIXES)
