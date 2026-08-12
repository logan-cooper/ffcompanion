"""Packaging and distribution.

The audience for this phase is a leaguemate who has never seen the code. These
tests assert the properties that make that first run survivable — the scripts
exist and parse, the promises in the README are true, and nothing anywhere
implies a cost.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ["scripts/setup.sh", "scripts/refresh.sh"]


@pytest.mark.parametrize("script", SCRIPTS)
def test_scripts_parse(script):
    """A syntax error here is a first-run failure for someone with no context."""
    result = subprocess.run(
        ["bash", "-n", str(ROOT / script)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", SCRIPTS)
def test_scripts_fail_loudly(script):
    """`set -euo pipefail`, so a failed step stops rather than reporting success
    over a half-built install."""
    assert "set -euo pipefail" in (ROOT / script).read_text()


def test_setup_asks_before_downloading_five_gigabytes():
    """The roadmap's own instruction: tell the user before it starts, not
    during. A surprise 5GB download on a phone tether is a bad first impression."""
    body = (ROOT / "scripts/setup.sh").read_text()

    banner = body.split("Continue?")[0]
    assert "5.2 GB" in banner or "5.5 GB" in banner, "size must precede the prompt"
    assert "Continue?" in body


def test_setup_states_the_hardware_floor():
    """~16GB is a real floor and an 8GB machine will have a bad time. Saying so
    before the download beats letting them find out after it."""
    body = (ROOT / "scripts/setup.sh").read_text()
    assert "RAM_FLOOR_GB=16" in body
    assert "swap" in body, "explain what happens below the floor, not just that it is low"


def test_setup_is_idempotent_by_checking_before_acting():
    """Re-running must not re-download or re-prompt."""
    body = (ROOT / "scripts/setup.sh").read_text()
    for guard in ("already downloaded", "already built", "already linked"):
        assert guard in body


def test_setup_queries_the_database_rather_than_grepping_cli_output():
    """The first version grepped `status` output for the word "league", which
    that output never contains — so setup was not idempotent. Parsing a
    human-readable table is the same mistake as asserting on a label."""
    body = (ROOT / "scripts/setup.sh").read_text()
    assert "COUNT(*)" in body
    assert "cli status" not in body


def test_setup_records_the_username_by_value_not_by_key():
    """.env.example ships `SLEEPER_USERNAME=`, so checking for the KEY always
    matched and the name was silently never recorded — a bug only a real
    fresh-clone run surfaced."""
    body = (ROOT / "scripts/setup.sh").read_text()
    assert "'^SLEEPER_USERNAME=..*'" in body, "must test for a non-empty value"


def test_setup_records_the_username_even_when_already_linked():
    """An install linked by hand, or predating this script, still needs the name
    on file or `make refresh` cannot re-pull rosters."""
    body = (ROOT / "scripts/setup.sh").read_text()
    linked_branch = body.split("a league is already linked")[1].split("elif")[0]
    assert "remember_username" in linked_branch


def test_refresh_uses_the_nfl_season_not_the_calendar_year():
    """An NFL season is named for the year it starts and opens in September, so
    `date +%Y` in August asks for a season that does not exist and 404s."""
    body = (ROOT / "scripts/refresh.sh").read_text()
    assert "default_season" in body
    assert "-lt 9" in body, "September is the cutover"


def test_refresh_explains_a_season_that_has_not_started():
    body = (ROOT / "scripts/refresh.sh").read_text()
    assert "No published stats" in body
    assert "untouched" in body, "reassure that existing data survives"


# --------------------------------------------------------------------- make

def test_make_targets_exist():
    makefile = (ROOT / "Makefile").read_text()
    for target in ("setup:", "refresh:", "serve:", "chat:", "warehouse:"):
        assert target in makefile


def test_every_make_target_is_documented():
    """`make help` is the discovery surface for someone who just cloned this."""
    makefile = (ROOT / "Makefile").read_text()
    declared = set(re.findall(r"^\.PHONY:(.*)$", makefile, re.M)[0].split())
    for target in declared - {"help"}:
        pattern = rf"^{re.escape(target)}:.*##"
        assert re.search(pattern, makefile, re.M), f"{target} has no help text"


# ----------------------------------------------------- the fresh-install path

def test_a_fresh_install_resolves_a_roster_without_team_intent():
    """THE Phase 8 gate, and it failed the first time it was run for real.

    roster_id came only from team_intent, which a fresh install has no rows in,
    so "how does my roster look?" was answered with "please provide your
    roster_id" — a question the user cannot answer and the loop is supposed to
    bind for them. Sleeper knows the answer: league_rosters.owner_id joins to
    league_users.user_id, whose display_name is the username.
    """
    from advisor.cli import _owned_roster
    from advisor.db import query

    leagues = query("SELECT league_id FROM leagues LIMIT 1")
    if not leagues:
        pytest.skip("no league linked")

    from advisor.config import get_settings

    if not (get_settings().sleeper_username or "").strip():
        pytest.skip("SLEEPER_USERNAME not configured")

    assert _owned_roster(leagues[0]["league_id"]) is not None


def test_survival_leagues_are_not_the_default_landing_place():
    """A survival league has no persistent rosters, so defaulting a newcomer
    into one answers most of their questions with "that does not apply here"."""
    import inspect

    from advisor.cli import _pick_league

    source = inspect.getsource(_pick_league)
    assert "survival" in source


# ------------------------------------------------------------- the $0 promise

def test_no_api_key_anywhere():
    """The claim the whole project rests on. Asserted, not trusted."""
    offenders = []
    for path in list((ROOT / "src").rglob("*.py")) + list((ROOT / "scripts").glob("*.sh")):
        text = path.read_text()
        for match in re.finditer(r"api[_-]?key|ANTHROPIC|OPENAI|sk-[a-zA-Z0-9]{8}", text, re.I):
            line = text[: match.start()].count("\n") + 1
            context = text.splitlines()[line - 1]
            # Comments saying there is no API key are the point, not a violation.
            if re.search(r"no api key|without an api key", context, re.I):
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{line}: {context.strip()}")
    assert not offenders, "possible API key usage:\n" + "\n".join(offenders)


def test_readme_tells_a_newcomer_the_floor_and_the_first_command():
    readme = (ROOT / "README.md").read_text()
    assert "16GB" in readme
    assert "make setup" in readme
