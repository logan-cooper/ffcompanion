"""Agent layer and eval harness.

Everything here is a pure function, so none of it needs a model, a server, or
the database — which matters because DuckDB's exclusive lock means DB-backed
tests cannot run while a chat or eval session is live.
"""

from __future__ import annotations

import json

import pytest

from advisor.agent.backend import Reply, ToolCall
from advisor.agent.loop import MAX_ITERATIONS, Turn, _run_tool
from advisor.agent.ollama import _coerce_arguments, to_ollama_tools
from advisor.evals.runner import (
    CaseResult,
    _is_grounded,
    _numbers,
    check,
    load_cases,
    per_case_matrix,
    scoreboard,
)
from advisor.tools import TOOLS


# --------------------------------------------------------------- schema shape

def test_tool_schemas_translate_to_the_runtime_format():
    converted = to_ollama_tools(TOOLS)
    assert len(converted) == len(TOOLS)
    for original, translated in zip(TOOLS, converted):
        assert translated["type"] == "function"
        assert translated["function"]["name"] == original["name"]
        # `input_schema` becomes `parameters` — a runtime detail that must stay
        # inside the backend and never leak into tools/.
        assert translated["function"]["parameters"] == original["input_schema"]


def test_translation_is_pure():
    """Converting must not mutate the registry's schemas."""
    before = json.dumps(TOOLS, sort_keys=True)
    to_ollama_tools(TOOLS)
    assert json.dumps(TOOLS, sort_keys=True) == before


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"a": 1}, {"a": 1}),
        ('{"a": 1}', {"a": 1}),  # some models emit arguments as a JSON string
        ("not json", {}),
        ('"a string"', {}),  # valid JSON, wrong shape
        ("[1, 2]", {}),
        (None, {}),
        (17, {}),
    ],
)
def test_tool_arguments_coerce_without_raising(raw, expected):
    """A model emitting a JSON string here is common, not malformed — rejecting
    it would throw away usable calls."""
    assert _coerce_arguments(raw) == expected


# ------------------------------------------------------------------- the loop

def test_unknown_tool_comes_back_as_a_result_not_an_exception():
    """The model picked the name, so only the model can correct it. Crashing
    the turn would throw away a recoverable mistake."""
    call = ToolCall(id="1", name="no_such_tool", arguments={})
    payload, is_error = _run_tool(call, ctx=None)

    assert is_error
    body = json.loads(payload)
    assert "no_such_tool" in body["error"]
    # The recovery path: tell it what it could have called.
    assert "resolve_player" in body["detail"]


def test_bad_arguments_come_back_as_a_result():
    call = ToolCall(id="1", name="resolve_player", arguments={"wrong_arg": "x"})
    payload, is_error = _run_tool(call, ctx=None)

    assert is_error
    assert "detail" in json.loads(payload)


def test_reply_reports_whether_it_wants_tools():
    assert not Reply(text="done").wants_tools
    assert Reply(tool_calls=(ToolCall("1", "resolve_player", {}),)).wants_tools


def test_reply_totals_tokens():
    assert Reply(prompt_tokens=100, completion_tokens=25).total_tokens == 125


def test_iteration_cap_is_bounded():
    """No per-token cost locally, but a loop stuck on one tool is still broken."""
    assert 1 < MAX_ITERATIONS <= 12


def test_turn_summary_is_renderable():
    turn = Turn(text="hi", iterations=2, prompt_tokens=10, completion_tokens=5)
    turn.tool_calls.append(("resolve_player", {}))
    assert "resolve_player" in turn.tools_used
    assert "2 iter" in turn.summary()


# ------------------------------------------------------------------ grounding

@pytest.mark.parametrize(
    "number,corpus,grounded",
    [
        ("23.44", '{"points_per_game": 23.44}', True),
        ("23.4", '{"points_per_game": 23.44}', True),  # rounding is reading, not inventing
        ("23", '{"points_per_game": 23.0}', True),
        ("99.9", '{"points_per_game": 23.44}', False),  # fabricated
        ("1793", '{"receiving_yards": 1793.0}', True),
    ],
)
def test_grounding_allows_rounding_but_not_invention(number, corpus, grounded):
    assert _is_grounded(number, corpus) is grounded


def test_only_meaningful_numbers_are_audited():
    """Bare small integers ("top 5", "3 games") are too common to attribute."""
    found = _numbers("He is top 5 with 23.44 ppg over 3 games and 1793 yards")
    assert "23.44" in found and "1793" in found
    assert "5" not in found and "3" not in found


def test_years_are_exempt_from_grounding():
    assert "2025" not in _numbers("in 2025 he had 23.44 ppg")


# -------------------------------------------------------------- case checking

def _turn(text: str, tools: list[str] | None = None, **kw) -> Turn:
    turn = Turn(text=text, **kw)
    for name in tools or []:
        turn.tool_calls.append((name, {}))
    return turn


def _tool_messages(payload: str) -> list[dict]:
    return [{"role": "tool", "content": payload}]


def test_fabricated_number_fails_the_case():
    case = {"id": "x", "ask": "?", "grounded": True}
    result = check(
        case,
        _turn("He averaged 99.9 points per game."),
        _tool_messages('{"points_per_game": 23.44}'),
    )
    assert not result.passed
    assert "99.9" in result.ungrounded


def test_grounded_number_passes():
    case = {"id": "x", "ask": "?", "grounded": True}
    result = check(
        case,
        _turn("He averaged 23.4 points per game."),
        _tool_messages('{"points_per_game": 23.44}'),
    )
    assert result.passed
    assert result.grounding_rate == 1.0


def test_missing_expected_tool_fails():
    case = {"id": "x", "ask": "?", "expect_tools": ["resolve_player"]}
    result = check(case, _turn("Sure.", tools=["get_my_roster"]), [])
    assert not result.passed
    assert any("missing tool" in f for f in result.failures)


def test_wants_pick_rejects_a_hedge():
    case = {"id": "x", "ask": "?", "wants_pick": True, "grounded": False}
    hedged = check(case, _turn("Both players have merit; it depends."), [])
    committed = check(case, _turn("Start Puka Nacua."), [])
    assert not hedged.passed
    assert committed.passed


def test_must_say_any_accepts_equivalent_wordings():
    """Assert on behaviour, not vocabulary."""
    case = {
        "id": "x",
        "ask": "?",
        "grounded": False,
        "must_say_any": ["no player", "not match", "couldn't find"],
    }
    assert check(case, _turn("That does not match any player."), []).passed
    assert check(case, _turn("I couldn't find them."), []).passed
    assert not check(case, _turn("He averaged well."), []).passed


def test_empty_answer_fails():
    assert not check({"id": "x", "ask": "?"}, _turn("   "), []).passed


def test_hitting_the_iteration_cap_fails():
    turn = _turn("gave up", hit_iteration_cap=True)
    assert not check({"id": "x", "ask": "?", "grounded": False}, turn, []).passed


# --------------------------------------------------------------- the case file

def test_shipped_cases_are_wellformed():
    cases = load_cases()
    assert len(cases) >= 10
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"

    known_tools = {t["name"] for t in TOOLS}
    for case in cases:
        assert case.get("ask"), case["id"]
        for tool in case.get("expect_tools", []):
            assert tool in known_tools, f"{case['id']} expects unknown tool {tool}"


def test_the_suite_covers_the_documented_failure_modes():
    """These are the cases the roadmap says matter most."""
    ids = {c["id"] for c in load_cases()}
    assert "nonexistent_player" in ids, "must test that it refuses to invent a player"
    assert "ambiguous_name" in ids, "name ambiguity is the documented #1 failure mode"
    assert "adversarial_premise" in ids


# ------------------------------------------------------------------ scoreboard

def _result(case_id: str, passed: bool, grounding: float = 1.0) -> CaseResult:
    return CaseResult(
        id=case_id,
        passed=passed,
        grounding_rate=grounding,
        seconds=30.0,
        ungrounded=[] if grounding == 1.0 else ["99.9"],
        failures=[] if passed else ["missing tool resolve_player"],
    )


def test_scoreboard_ranks_on_tool_accuracy_then_grounding():
    runs = {
        "weak": [_result("a", False), _result("b", True, 0.5)],
        "strong": [_result("a", True), _result("b", True)],
    }
    assert "Best on tool accuracy" in scoreboard(runs)
    assert scoreboard(runs).rstrip().endswith("strong")


def test_scoreboard_counts_fabrications_separately_from_failures():
    runs = {"m": [_result("a", True, 0.5)]}
    # Grounding is reported even when the case otherwise passed.
    assert "50%" in scoreboard(runs)


def test_per_case_matrix_header_fits_short_case_names():
    runs = {"m": [_result("a", True)]}
    header = per_case_matrix(runs).splitlines()[1]
    assert header.startswith("case")
    assert "casem" not in header  # the column must not run into the model name
