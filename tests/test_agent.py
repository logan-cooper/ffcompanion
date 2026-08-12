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
from advisor.agent.ollama import (
    EVAL_SEED,
    MIN_CONTEXT_TOKENS,
    OllamaBackend,
    _coerce_arguments,
    to_ollama_tools,
)
from advisor.evals.runner import (
    DEFAULT_MAX_SECONDS,
    CaseResult,
    _is_grounded,
    _numbers,
    _paired_format_failures,
    check,
    load_cases,
    per_case_matrix,
    report,
    scoreboard,
)
from advisor.tools import TOOLS
from advisor.tools.registry import coerce_arguments


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


# --------------------------------------------------------------- context window

class _FakeResponse:
    status_code = 200

    def json(self) -> dict:
        return {"message": {"role": "assistant", "content": "ok"}}


def _capture_payload(monkeypatch) -> dict:
    """Run one chat() and return the payload that would have gone over HTTP."""
    captured: dict = {}

    def fake_post(url, json=None, timeout=None):
        captured.update(json or {})
        return _FakeResponse()

    monkeypatch.setattr("advisor.agent.ollama.requests.post", fake_post)
    OllamaBackend(model="test:8b").chat(system="s", messages=[], tools=[])
    return captured


def test_context_window_is_requested_explicitly(monkeypatch):
    """Ollama defaults to 4096 and silently truncates past it — so a turn that
    overflows can drop the very prompt rule that keeps numbers honest."""
    options = _capture_payload(monkeypatch)["options"]
    assert options["num_ctx"] >= MIN_CONTEXT_TOKENS


def test_context_window_never_falls_below_the_floor(monkeypatch):
    """A too-small override is a silent correctness bug, so it gets clamped."""
    backend = OllamaBackend(model="test:8b")
    backend.context_tokens = 512

    captured: dict = {}
    monkeypatch.setattr(
        "advisor.agent.ollama.requests.post",
        lambda url, json=None, timeout=None: (captured.update(json), _FakeResponse())[1],
    )
    backend.chat(system="s", messages=[], tools=[])
    assert captured["options"]["num_ctx"] == MIN_CONTEXT_TOKENS


def test_chat_does_not_pin_the_seed(monkeypatch):
    """A user who rephrases a question and gets a byte-identical answer back is
    being failed by a frozen seed, so only evals pin one."""
    assert "seed" not in _capture_payload(monkeypatch)["options"]


def test_evals_pin_the_seed(monkeypatch):
    """Evals pick the model here; a one-run-each comparison must not measure
    sampling luck."""
    captured: dict = {}
    monkeypatch.setattr(
        "advisor.agent.ollama.requests.post",
        lambda url, json=None, timeout=None: (captured.update(json), _FakeResponse())[1],
    )
    OllamaBackend(model="test:8b", seed=EVAL_SEED).chat(system="s", messages=[], tools=[])
    assert captured["options"]["seed"] == EVAL_SEED


def test_thinking_is_off_by_default(monkeypatch):
    """Measured, not assumed: 12/12 in 4.3 min with thinking off against 11/12
    in 15.1 min with it on — better on accuracy AND speed."""
    assert _capture_payload(monkeypatch)["think"] is False


def test_thinking_can_be_turned_back_on(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "advisor.agent.ollama.requests.post",
        lambda url, json=None, timeout=None: (captured.update(json), _FakeResponse())[1],
    )
    backend = OllamaBackend(model="test:8b", think=True)
    backend.chat(system="s", messages=[], tools=[])

    assert captured["think"] is True
    # The label has to carry it, or two rows of a scoreboard look identical.
    assert "no-think" in OllamaBackend(model="test:8b", think=False).name
    assert "no-think" not in backend.name


def test_the_floor_clears_the_fixed_overhead():
    """Six tool schemas plus the system prompt cost ~2k tokens before the user
    has said anything; the window has to leave real room for tool results."""
    schema_tokens = len(json.dumps(to_ollama_tools(TOOLS))) // 4
    assert MIN_CONTEXT_TOKENS > schema_tokens * 4


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


# ------------------------------------------------------- argument type fitting

def test_string_integers_are_fitted():
    """weeks="8" reached min(weeks, MAX_WEEKS) and raised TypeError, costing
    llama3.1:8b three eval cases — our bug, scored against the model."""
    assert coerce_arguments("compare_players", {"weeks": "8"})["weeks"] == 8


def test_a_bare_value_becomes_a_list_where_one_is_wanted():
    fitted = coerce_arguments("compare_players", {"player_ids": "00-0038542"})
    assert fitted["player_ids"] == ["00-0038542"]

    both = coerce_arguments("compare_players", {"player_ids": "00-01, 00-02"})
    assert both["player_ids"] == ["00-01", "00-02"]


def test_a_correct_call_is_left_alone():
    original = {"player_ids": ["00-01", "00-02"], "weeks": 8}
    assert coerce_arguments("compare_players", original) == original


def test_unconvertible_values_pass_through_untouched():
    """The tool's own error beats a guess — the model can correct itself."""
    assert coerce_arguments("compare_players", {"weeks": "lots"})["weeks"] == "lots"


def test_unknown_arguments_are_not_dropped():
    """Dropping them would hide the mistake from the model that made it."""
    assert coerce_arguments("compare_players", {"nonsense": 1})["nonsense"] == 1


def test_fitting_an_unknown_tool_is_harmless():
    assert coerce_arguments("no_such_tool", {"a": "1"}) == {"a": "1"}


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


@pytest.mark.parametrize(
    "number,corpus,grounded",
    [
        # The real case: compare_players returns usage as a fraction, and the
        # model wrote "78.5% snap share". Reading, not inventing.
        ("78.5", '{"usage": 0.785}', True),
        ("79", '{"usage": 0.785}', True),  # rounded to a whole percent
        ("65", '{"usage": 0.785}', False),  # a rate that is simply not there
        # Must not let a large fabricated number match an unrelated small one.
        ("305", '{"win_now": 3.05}', False),
    ],
)
def test_percentages_read_off_fractions_are_grounded(number, corpus, grounded):
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


_PICK_CASE = {"id": "x", "ask": "?", "wants_pick": True, "grounded": False}
_WAIVER_CORPUS = _tool_messages(
    '{"players": [{"name": "Ray Davis"}, {"name": "Phil Mafah"}]}'
)


def test_wants_pick_rejects_a_hedge():
    hedged = check(
        _PICK_CASE,
        _turn("Ray Davis and Phil Mafah both have merit; it depends on your needs."),
        _WAIVER_CORPUS,
    )
    assert not hedged.passed
    assert any("hedged" in f for f in hedged.failures)


def test_wants_pick_accepts_a_commitment_it_has_no_word_for():
    """The real answer that was scored as no recommendation: it matched none of
    the original fifteen marker words."""
    answer = (
        "The best available RB on waivers is Ray Davis (BUF, 4.12 PPG). "
        "Prioritize Ray Davis for his consistent production."
    )
    assert check(_PICK_CASE, _turn(answer), _WAIVER_CORPUS).passed


def test_wants_pick_rejects_an_answer_that_names_nobody():
    vague = check(
        _PICK_CASE,
        _turn("There are several solid options on the wire this week."),
        _WAIVER_CORPUS,
    )
    assert not vague.passed
    assert any("named nobody" in f for f in vague.failures)


def test_a_commitment_survives_hedging_language_around_it():
    """Acknowledging a tradeoff is not refusing to choose."""
    answer = "It depends on your needs, but I'd start Ray Davis."
    assert check(_PICK_CASE, _turn(answer), _WAIVER_CORPUS).passed


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


def test_slowness_is_flagged_but_does_not_fail_a_correct_answer():
    """start_sit "passed" in 288s and the report called it clean, so slowness
    must be visible — but the same model at the same seed ran 3-5x slower on a
    busy laptop, so gating on wall clock would fail cases for being unlucky."""
    slow = check(
        {"id": "x", "ask": "?", "grounded": False},
        _turn("Start Puka Nacua.", elapsed_seconds=288.0),
        [],
    )
    assert slow.too_slow
    assert slow.passed, "wall clock is not reproducible enough to gate on"


def test_a_case_can_justify_a_bigger_budget():
    """A two-turn case legitimately takes two turns' worth of time."""
    turn = _turn("Start Puka Nacua.", elapsed_seconds=200.0)
    case = {"id": "x", "ask": "?", "grounded": False, "max_seconds": 240}
    assert not check(case, turn, []).too_slow


def test_a_slow_pass_is_visible_in_both_reports():
    slow = CaseResult(id="start_sit", passed=True, too_slow=True, seconds=288.0)
    assert "SLOW" in report([slow], "m")
    assert "not worth waiting for" in report([slow], "m")
    assert "slow" in scoreboard({"m": [slow]})


def test_shipped_cases_stay_inside_their_budgets():
    """A budget nobody can meet is a broken assertion, not a standard."""
    for case in load_cases():
        budget = case.get("max_seconds", DEFAULT_MAX_SECONDS)
        assert 0 < budget <= 300, f"{case['id']} budget {budget} exceeds the HTTP timeout"


def test_backend_failure_is_not_scored_as_a_fabrication():
    """A timeout message carries the port (11434) and the timeout (300). Audited
    as an answer, those read as invented statistics — which is how a runtime
    problem disguises itself as a model problem."""
    turn = _turn("The model backend failed:\nRead timed out. (read timeout=300)")
    turn.errors.append("Ollama request failed: port=11434 read timeout=300")

    result = check({"id": "x", "ask": "?", "grounded": True}, turn, [])

    assert not result.passed
    assert result.infrastructure_error
    assert not result.ungrounded, "must not blame the model for a timeout"
    assert any("backend error" in f for f in result.failures)


def test_failures_carry_their_answer_text_for_diagnosis():
    """'invented 78.5' is not actionable without the sentence it appeared in."""
    from advisor.evals.runner import as_json

    failed = CaseResult(id="a", passed=False, answer="He averaged 78.5 ppg.")
    payload = json.loads(as_json([failed, _result("b", True)], "m"))

    assert payload["cases"][0]["answer"] == "He averaged 78.5 ppg."
    assert "answer" not in payload["cases"][1], "passing cases stay compact"


def test_a_finished_model_run_survives_a_restart(tmp_path):
    """45 minutes of local inference should not be lost to closing a laptop."""
    from advisor.evals.runner import load_run, save_run

    original = [_result("a", True), _result("b", False, 0.5)]
    save_run(tmp_path, "qwen3:8b", original)
    restored = load_run(tmp_path, "qwen3:8b", expected_cases=2)

    assert restored is not None
    assert [r.id for r in restored] == ["a", "b"]
    assert [r.passed for r in restored] == [True, False]
    assert restored[1].grounding_rate == 0.5, "grounding must survive the round trip"


def test_a_cached_run_from_a_different_suite_is_rejected(tmp_path):
    """Comparing models on different questions is worse than re-running."""
    from advisor.evals.runner import load_run, save_run

    save_run(tmp_path, "qwen3:8b", [_result("a", True)])
    assert load_run(tmp_path, "qwen3:8b", expected_cases=12) is None


def test_missing_cache_is_not_an_error(tmp_path):
    from advisor.evals.runner import load_run

    assert load_run(tmp_path, "never-run:8b", expected_cases=12) is None


def test_report_flags_unmeasured_cases():
    broken = CaseResult(id="a", passed=False, infrastructure_error=True)
    assert "NOT MEASURED" in report([broken], "m")
    assert "NOT MEASURED" not in report([_result("a", True)], "m")


def test_scoreboard_warns_when_a_comparison_is_unsound():
    broken = CaseResult(id="a", passed=False, infrastructure_error=True)
    assert "WARNING" in scoreboard({"m": [broken]})
    assert "WARNING" not in scoreboard({"m": [_result("a", True)]})


def test_empty_answer_fails():
    assert not check({"id": "x", "ask": "?"}, _turn("   "), []).passed


def test_hitting_the_iteration_cap_fails():
    turn = _turn("gave up", hit_iteration_cap=True)
    assert not check({"id": "x", "ask": "?", "grounded": False}, turn, []).passed


# -------------------------------------------------------- known-correct answers

_TRADE_CASE = {
    "id": "x",
    "ask": "?",
    "grounded": False,
    "correct_answer": {
        "summary": "make the trade",
        "any_of": ["make the trade", "accept", "do it", "trade him"],
        "none_of": ["keep mccaffrey", "avoid the trade", "decline"],
    },
}


def test_a_wrong_conclusion_fails_even_when_well_argued():
    """The real failure: it said "prioritise future value", noted the trade
    raised future by 17.53, then advised against it. Every other assertion
    passed."""
    wrong = check(
        _TRADE_CASE,
        _turn(
            "This raises your future value by 17.53. Since you're rebuilding, "
            "prioritize future value. Keep McCaffrey and avoid the trade."
        ),
        [],
    )
    assert not wrong.passed
    assert any("wrong answer" in f for f in wrong.failures)


def test_the_right_conclusion_passes_in_any_wording():
    for phrasing in ("Make the trade.", "I'd accept this one.", "Trade him."):
        assert check(_TRADE_CASE, _turn(phrasing), []).passed, phrasing


def test_cases_without_a_known_answer_are_unaffected():
    assert check({"id": "x", "ask": "?", "grounded": False}, _turn("Anything."), []).passed


# ------------------------------------------------------------- format pairing

def _paired(dynasty_text: str, redraft_text: str, redraft_corpus: str = '{"future": 0.0}'):
    return {
        "dynasty": (_turn(dynasty_text), _tool_messages('{"future": 108.7}')),
        "redraft": (_turn(redraft_text), _tool_messages(redraft_corpus)),
    }


def test_identical_answers_across_formats_fail():
    """The whole point of Phase 3b. If dynasty and redraft answer the same, the
    format never reached the answer and every other case still looks fine."""
    case = {"id": "x", "expects_different_answer": True}
    same = _paired("Trade him.", "Trade him.")
    failures = _paired_format_failures(case, same)
    assert any("identical answer" in f for f in failures)


def test_differing_answers_across_formats_pass():
    case = {"id": "x", "expects_different_answer": True}
    differing = _paired("Hold him for the future.", "Trade him, age is irrelevant.")
    assert _paired_format_failures(case, differing) == []


def test_a_single_year_format_must_price_no_future():
    """future is zero in redraft by definition, so a non-zero one is wrong, not
    a judgement call."""
    case = {"id": "x"}
    leaked = _paired("Hold.", "Trade.", redraft_corpus='{"future": 108.7}')
    failures = _paired_format_failures(case, leaked)
    assert any("future=108.7" in f for f in failures)


def test_a_verdict_that_fails_to_invert_is_caught():
    """Two answers can differ from each other and both be wrong. This league's
    own weights say accept in dynasty and decline in redraft."""
    case = {
        "id": "x",
        "correct_answer_by_format": {
            "dynasty": {"summary": "accept", "any_of": ["accept", "make the trade"]},
            "redraft": {"summary": "decline", "any_of": ["decline", "keep"]},
        },
    }
    backwards = _paired("Decline, keep McCaffrey.", "Accept, make the trade.")
    failures = _paired_format_failures(case, backwards)
    assert any("dynasty: wrong answer" in f for f in failures)
    assert any("redraft: wrong answer" in f for f in failures)

    correct = _paired("Accept — make the trade.", "Decline, keep McCaffrey.")
    assert _paired_format_failures(case, correct) == []


def test_multi_year_formats_are_allowed_a_future():
    case = {"id": "x"}
    assert _paired_format_failures(case, _paired("Hold.", "Trade.")) == []


def test_reinterpreting_a_league_changes_only_the_format():
    """Same roster, scoring, week and players — so a differing answer is
    attributable to format and nothing else."""
    from advisor.context import LeagueContext
    from advisor.evals.runner import _as_format

    dynasty = LeagueContext(
        league_id="1", name="Test", season=2025, format="dynasty", current_week=14
    )
    redraft = _as_format(dynasty, "redraft")

    assert redraft.format == "redraft"
    assert redraft.current_week == dynasty.current_week
    assert redraft.league_id == dynasty.league_id
    assert "redraft" in redraft.name


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
