"""Eval harness.

With a hosted frontier model, evals catch regressions. Running locally, they do
something more load-bearing: **they pick the model.** Tool-call reliability
varies enormously across 7-8B models and cannot be judged by reputation, so
`make eval MODEL=x` is how the choice gets made with evidence.

Assertions are checkable properties, never exact wording. The most important one
is `grounded`: every number in the answer must appear in a tool result. This app
exists to make its numbers traceable, so a fabricated statistic is the worst
failure it can have — and unlike tone or length, it can be measured exactly.
"""

from __future__ import annotations

import dataclasses
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from advisor.agent import Turn, run_turn
from advisor.agent.backend import Backend
from advisor.context import LeagueContext
from advisor.league_format import MULTI_YEAR_FORMATS

CASES_PATH = Path(__file__).resolve().parents[3] / "evals" / "cases.yaml"

# Numbers worth checking. Bare small integers ("3 games", "top 5") are far too
# common to attribute, so only decimals and 2+ digit figures are audited.
NUMBER_PATTERN = re.compile(r"\d+\.\d+|\b\d{2,}\b")

# Years and roster/week counts are legitimately reasoned about rather than read
# off a tool result.
GROUNDING_EXEMPT = {"2023", "2024", "2025", "2026", "2027", "10", "12", "16", "17", "18"}

# A correct answer nobody waits for is not a working feature: start_sit once
# "passed" in 288s, twelve seconds under the HTTP timeout, and the report called
# it clean. So slowness is surfaced — but it is NOT a failure, because wall clock
# on a laptop is not reproducible. The same model at the same seed ran 3-5x
# slower while the machine was busy (ambiguous_name 30s -> 136s), so gating on it
# would fail cases for being unlucky and would corrupt exactly the model
# comparison the pinned seed exists to make fair. Report it, rank on it, don't
# fail on it. Cases can override with `max_seconds`.
DEFAULT_MAX_SECONDS = 120

# Half a percentage point, which is what rounding a fraction to a whole percent
# costs (0.785 -> "79%"), plus slack for binary floating point.
PERCENT_TOLERANCE = 0.0051

# Refusing to choose. These are the actual failure — a survey of the options
# with the decision handed back to the user.
HEDGE_MARKERS = (
    "it depends", "depends on", "both are", "both have", "either could",
    "either one", "hard to say", "up to you", "your call", "toss-up",
    "toss up", "personal preference", "no clear", "consider your",
)

# Committing to one. Deliberately broad, and only ever used to OVERRIDE a hedge
# — never as the test itself. A vocabulary list cannot decide whether a model
# answered; "Prioritize Ray Davis" matched none of the original fifteen words
# and was scored as giving no recommendation.
DECISION_MARKERS = (
    "i'd", "i would", "recommend", "go with", "pick", "choose", "prioriti",
    "best", "start ", "accept", "decline", "hold", "keep", "target", "add ",
    "drop ", "sit ", "take ", "edge", "over ", "instead", "the answer",
)

NAME_IN_TOOL_RESULT = re.compile(r'"name":\s*"([^"]{3,40})"')


@dataclass
class CaseResult:
    id: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    grounding_rate: float = 1.0
    ungrounded: list[str] = field(default_factory=list)
    tokens: int = 0
    seconds: float = 0.0
    answer: str = ""
    # The run never reached the model (timeout, server down). Reported apart
    # from model failures because the fix is a different one.
    infrastructure_error: bool = False
    # Answered correctly, but too slowly to be worth waiting for.
    too_slow: bool = False


def _numbers(text: str) -> list[str]:
    return [n for n in NUMBER_PATTERN.findall(text) if n not in GROUNDING_EXEMPT]


def _is_grounded(number: str, corpus: str) -> bool:
    """True if `number` traces to something a tool actually returned.

    Rounding is allowed: a model saying "23.4" off a tool's "23.44" is reading
    correctly, not inventing. Anything that matches nothing is a fabrication.
    """
    if number in corpus:
        return True
    try:
        value = float(number)
    except ValueError:
        return False
    # Accept the value appearing at other precisions, and as a plain integer.
    candidates = {f"{value:.1f}", f"{value:.2f}", str(int(value)) if value == int(value) else ""}
    if any(c and c in corpus for c in candidates):
        return True
    # Accept a tool number that rounds to this one (e.g. 23.44 -> "23.4").
    for found in re.findall(r"\d+\.\d+", corpus):
        try:
            if abs(float(found) - value) < 0.05 or round(float(found), 1) == value:
                return True
        except ValueError:
            continue

    # Rates come back as fractions (usage: 0.785) and get written as percentages
    # ("78.5% snap share"). That is unit conversion, the same kind of reading
    # rounding is — flagging it as invention would penalise a model for stating
    # a rate the way people actually say it. Bounded to plausible percentages so
    # a cited 305 cannot match a corpus 3.05, and toleranced to half a point so
    # rounding to a whole percent still counts.
    if 0 <= value <= 100:
        fraction = value / 100
        for found in re.findall(r"\d*\.\d+", corpus):
            try:
                if abs(float(found) - fraction) <= PERCENT_TOLERANCE:
                    return True
            except ValueError:
                continue
    return False


def _tool_corpus(messages: list[dict[str, Any]]) -> str:
    return "\n".join(m.get("content", "") for m in messages if m.get("role") == "tool")


def _says(answer: str, expected: dict) -> bool:
    """Is the model's conclusion the one we know to be right?

    Written as `any_of` / `none_of` phrase groups rather than one exact string,
    because there are many ways to say "make this trade" and testing for one of
    them tests phrasing again. The point is the verdict, not the sentence.
    """
    lowered = answer.lower()
    if any(p.lower() in lowered for p in expected.get("none_of", [])):
        return False
    wanted = expected.get("any_of", [])
    return not wanted or any(p.lower() in lowered for p in wanted)


def _recommendation_failures(answer: str, corpus: str) -> list[str]:
    """Did the model actually choose something?

    Tested behaviourally, because vocabulary does not work here: the model wrote
    "Prioritize Ray Davis" and a fifteen-word marker list scored it as giving no
    recommendation. What a real answer does is **name one of the options the
    tools returned** — which ties the check to the data rather than to phrasing,
    and is the same instinct as the grounding assertion.

    Hedging is then the separate failure: naming the candidates and handing the
    decision back ("both are solid, depends what you need").
    """
    named = [n for n in set(NAME_IN_TOOL_RESULT.findall(corpus)) if n.lower() in answer.lower()]
    if corpus and not named:
        return ["named nobody from the tool results"]

    lowered = answer.lower()
    if any(h in lowered for h in HEDGE_MARKERS) and not any(
        d in lowered for d in DECISION_MARKERS
    ):
        return ["hedged instead of committing"]

    # No tool corpus to check against (a refusal case), so fall back to language.
    if not corpus and not any(d in lowered for d in DECISION_MARKERS):
        return ["no recommendation given"]
    return []


def check(case: dict, turn: Turn, messages: list[dict]) -> CaseResult:
    result = CaseResult(
        id=case["id"],
        passed=True,
        tools_used=turn.tools_used,
        tokens=turn.prompt_tokens + turn.completion_tokens,
        seconds=turn.elapsed_seconds,
        answer=turn.text,
    )
    answer = turn.text or ""
    lowered = answer.lower()

    # A backend failure is not a model failure, and scoring it as one sends you
    # tuning a prompt when the actual problem is the runtime. It also poisons
    # the grounding audit: the error text carries a port and a timeout value,
    # which read as invented statistics and are nothing of the sort.
    if turn.errors:
        result.infrastructure_error = True
        result.failures.append(f"backend error: {turn.errors[0][:120]}")
        result.passed = False
        return result

    for tool in case.get("expect_tools", []):
        if tool not in turn.tools_used:
            result.failures.append(f"missing tool {tool}")

    for tool in case.get("forbid_tools", []):
        if tool in turn.tools_used:
            result.failures.append(f"called forbidden tool {tool}")

    for needle in case.get("must_say", []):
        if needle.lower() not in lowered:
            result.failures.append(f"missing phrase {needle!r}")

    # Assert on behaviour, not vocabulary. "does not match any player" and
    # "couldn't find that player" are the same correct answer; requiring one
    # exact wording tests the phrasing rather than the model.
    alternatives = case.get("must_say_any", [])
    if alternatives and not any(n.lower() in lowered for n in alternatives):
        result.failures.append(f"said none of {alternatives}")

    for needle in case.get("must_not_say", []):
        if needle.lower() in lowered:
            result.failures.append(f"said forbidden {needle!r}")

    if case.get("wants_pick"):
        result.failures.extend(_recommendation_failures(answer, _tool_corpus(messages)))

    # A known-correct answer. Most assertions here check that the model behaved
    # sensibly; this one checks that it was RIGHT. It exists because a paired
    # trade case passed every other check while concluding the opposite of its
    # own reasoning — "prioritise future value", then declining a trade that
    # raised future value by 17.53.
    expected = case.get("correct_answer")
    if expected and not _says(answer, expected):
        result.failures.append(f"wrong answer: expected {expected['summary']!r}")

    if not answer.strip():
        result.failures.append("empty answer")

    if case.get("grounded", True):
        corpus = _tool_corpus(messages)
        numbers = _numbers(answer)
        bad = [n for n in numbers if not _is_grounded(n, corpus)]
        result.ungrounded = bad
        result.grounding_rate = 1.0 - (len(bad) / len(numbers)) if numbers else 1.0
        if bad:
            result.failures.append(f"ungrounded numbers: {bad[:6]}")

    if turn.hit_iteration_cap:
        result.failures.append("hit iteration cap")

    result.too_slow = turn.elapsed_seconds > case.get("max_seconds", DEFAULT_MAX_SECONDS)

    result.passed = not result.failures
    return result


def load_cases(path: Path | None = None) -> list[dict]:
    return yaml.safe_load((path or CASES_PATH).read_text())


def _ask(backend: Backend, ctx: LeagueContext, case: dict) -> tuple[Turn, list[dict]]:
    messages: list[dict] = [{"role": "user", "content": case["ask"]}]
    turn = run_turn(backend, ctx, messages)

    if case.get("followup"):
        messages.append({"role": "user", "content": case["followup"]})
        second = run_turn(backend, ctx, messages)
        # Judge the pair as one interaction: tools from either turn count.
        turn.tool_calls += second.tool_calls
        turn.text = second.text
        turn.prompt_tokens += second.prompt_tokens
        turn.completion_tokens += second.completion_tokens
        turn.elapsed_seconds += second.elapsed_seconds
        turn.hit_iteration_cap = turn.hit_iteration_cap or second.hit_iteration_cap

    return turn, messages


def _as_format(ctx: LeagueContext, fmt: str) -> LeagueContext:
    """The same league, reinterpreted under another format.

    Deliberately NOT a second real league. Holding the roster, scoring, week and
    player pool fixed means any difference in the answer is attributable to
    format and nothing else; a different league would vary all of those at once
    and prove nothing. Same technique tools/demo.py uses to prove the tool layer
    branches — this applies it one level up, to the agent.
    """
    return dataclasses.replace(ctx, format=fmt, name=f"{ctx.name} (as {fmt})")


def _at_week(ctx: LeagueContext, week: int) -> LeagueContext:
    """The same league at a different point in the year.

    Season phase is derived from current_week — 0 is the offseason, 18+ is a
    finished season — so moving the week is enough to exercise every branch of
    Phase 3c without waiting for the calendar. This matters more than it sounds:
    dynasty leagues trade hardest Feb-Aug, when the season being valued has zero
    games played, and that path is otherwise never tested.
    """
    return dataclasses.replace(ctx, current_week=week, name=f"{ctx.name} (week {week})")


def _variants(ctx: LeagueContext, case: dict) -> dict[str, LeagueContext] | None:
    """The context variants a paired case asks for, keyed by label."""
    if formats := case.get("paired_formats"):
        return {fmt: _as_format(ctx, fmt) for fmt in formats}
    if weeks := case.get("paired_weeks"):
        return {f"week{w}": _at_week(ctx, w) for w in weeks}
    return None


def run_case(backend: Backend, ctx: LeagueContext, case: dict) -> CaseResult:
    variants = _variants(ctx, case)
    if not variants:
        turn, messages = _ask(backend, ctx, case)
        return check(case, turn, messages)

    # Ask the identical question under each variant and compare.
    runs = {label: _ask(backend, variant, case) for label, variant in variants.items()}

    primary = next(iter(runs))
    turn, messages = runs[primary]
    result = check(case, turn, messages)
    result.seconds = sum(t.elapsed_seconds for t, _ in runs.values())
    result.failures.extend(_paired_format_failures(case, runs))
    result.failures.extend(_paired_week_failures(case, runs))
    result.passed = not result.failures
    return result


def _paired_week_failures(
    case: dict, runs: dict[str, tuple[Turn, list[dict]]]
) -> list[str]:
    """Check that where we are in the year reached the answer.

    Phase 3c's premise is that the same question is answered differently in
    March than in December — off a prior-season baseline rather than this
    season's games. Nothing verified that through the agent.
    """
    if not case.get("paired_weeks"):
        return []

    failures = []
    for label, (turn, messages) in runs.items():
        answer = (turn.text or "").lower()
        corpus = _tool_corpus(messages)

        # With no games left win_now is zero for EVERYONE by arithmetic. A model
        # reading that as a verdict on a player is the exact failure the
        # envelope's win_now caveat exists to prevent.
        if '"win_now": 0' in corpus or '"win_now": 0.0' in corpus:
            for slur in ("worthless", "no value", "has no value", "zero value"):
                if slur in answer:
                    failures.append(f"{label}: read win_now=0 as a verdict ({slur!r})")
                    break

        # In the offseason every number is last season's, aged forward. Passing
        # it off as this season's is the misread that matters.
        offseason = re.search(r'"season_started":\s*false', corpus)
        if offseason and any(
            p in answer for p in ("this season he", "so far this season", "this year he")
        ):
            failures.append(f"{label}: presented prior-season stats as current")

    return failures


def _paired_format_failures(
    case: dict, runs: dict[str, tuple[Turn, list[dict]]]
) -> list[str]:
    """Check that format actually reached the answer.

    This is the highest-value assertion in the suite. Phase 3b exists so that a
    dynasty answer and a redraft answer to the same question differ; if they come
    back identical, something upstream collapsed and every other case would still
    look fine.

    Only for format-paired cases. Without this guard the labels of a WEEK-paired
    case ("week0", "week14") are read as format names, none of them match a
    multi-year format, and a dynasty league gets failed for the future values it
    is supposed to have.
    """
    if not case.get("paired_formats"):
        return []

    failures = []

    # Each format must produce its own answer, not one answer twice.
    if case.get("expects_different_answer"):
        texts = {fmt: (turn.text or "").strip() for fmt, (turn, _) in runs.items()}
        if len(set(texts.values())) < len(texts):
            failures.append("identical answer across formats — format never reached it")

    # In a single-year format future value is zero by definition, so citing a
    # non-zero one is not a judgement call, it is wrong.
    for fmt, (turn, messages) in runs.items():
        if fmt in MULTI_YEAR_FORMATS:
            continue
        corpus = _tool_corpus(messages)
        for match in re.finditer(r'"future":\s*([0-9.]+)', corpus):
            if float(match.group(1)) != 0.0:
                failures.append(f"{fmt}: tool returned future={match.group(1)}, must be 0")
                break

    # The strongest assertion available: the right verdict INVERTS between
    # formats, and we know which way round because the app's own weights say so.
    # A model can produce two different-sounding answers that are both wrong;
    # this is what catches that.
    for fmt, expected in (case.get("correct_answer_by_format") or {}).items():
        turn, _ = runs.get(fmt, (None, None))
        if turn is None:
            continue
        if not _says(turn.text or "", expected):
            failures.append(f"{fmt}: wrong answer, expected {expected['summary']!r}")

    return failures


def run_suite(
    backend: Backend,
    ctx: LeagueContext,
    cases: list[dict] | None = None,
    *,
    on_result=lambda _: None,
) -> list[CaseResult]:
    results = []
    for case in cases if cases is not None else load_cases():
        started = time.monotonic()
        try:
            result = run_case(backend, ctx, case)
        except Exception as exc:  # a crash is a failure, not a stopped suite
            result = CaseResult(
                id=case["id"],
                passed=False,
                failures=[f"raised {type(exc).__name__}: {exc}"],
                seconds=time.monotonic() - started,
            )
        results.append(result)
        on_result(result)
    return results


def report(results: list[CaseResult], label: str) -> str:
    passed = sum(r.passed for r in results)
    total = len(results)
    tokens = sum(r.tokens for r in results)
    seconds = sum(r.seconds for r in results)
    grounding = (
        sum(r.grounding_rate for r in results) / total if total else 0.0
    )
    tool_failures = sum(
        1 for r in results if any(f.startswith("missing tool") for f in r.failures)
    )
    fabrications = sum(1 for r in results if r.ungrounded)

    lines = [
        "",
        "=" * 74,
        f"{label}",
        "=" * 74,
        f"{'case':<22}{'result':<8}{'tools':<34}{'sec':>6}",
        "-" * 74,
    ]
    for r in results:
        mark = "pass" if r.passed else "FAIL"
        slow = " SLOW" if r.too_slow else ""
        lines.append(
            f"{r.id:<22}{mark:<8}{','.join(r.tools_used)[:32]:<34}{r.seconds:>6.1f}{slow}"
        )
        for failure in r.failures:
            lines.append(f"{'':22}  - {failure}")

    broken = sum(1 for r in results if r.infrastructure_error)

    lines += [
        "-" * 74,
        f"passed             {passed}/{total}",
        f"correct tool use   {total - tool_failures}/{total}",
        f"grounding          {grounding:.1%}  ({fabrications} case(s) with invented numbers)",
        f"tokens             {tokens:,}",
        f"wall clock         {seconds:.0f}s   ($0.00 — runs on your machine)",
    ]
    if broken:
        lines.append(
            f"NOT MEASURED       {broken} case(s) failed on the backend, not the "
            f"model — fix those before reading the score above"
        )
    slow = [r for r in results if r.too_slow]
    if slow:
        worst = max(slow, key=lambda r: r.seconds)
        lines.append(
            f"slow               {len(slow)} case(s) over budget, worst "
            f"{worst.id} at {worst.seconds:.0f}s (correct, but not worth waiting for)"
        )
    lines.append("")
    return "\n".join(lines)


def scoreboard(runs: dict[str, list[CaseResult]]) -> str:
    """Compare models side by side. This is the artifact that picks one.

    Ordered by the two things that decide it: whether the model calls the right
    tools, and whether it makes numbers up. Speed is reported but is a tiebreak —
    a fast model that fabricates statistics is useless to this app.
    """
    lines = [
        "",
        "=" * 78,
        "MODEL COMPARISON",
        "=" * 78,
        f"{'model':<20}{'passed':>9}{'tools ok':>10}{'grounded':>10}"
        f"{'fabricated':>12}{'slow':>6}{'min':>7}",
        "-" * 78,
    ]
    ranked = []
    for label, results in runs.items():
        total = len(results) or 1
        passed = sum(r.passed for r in results)
        tools_ok = sum(
            1
            for r in results
            if not any(f.startswith("missing tool") for f in r.failures)
        )
        grounding = sum(r.grounding_rate for r in results) / total
        fabricated = sum(1 for r in results if r.ungrounded)
        slow = sum(1 for r in results if r.too_slow)
        minutes = sum(r.seconds for r in results) / 60
        ranked.append((tools_ok, grounding, passed, -minutes, label))
        lines.append(
            f"{label[:19]:<20}{f'{passed}/{total}':>9}{f'{tools_ok}/{total}':>10}"
            f"{grounding:>9.0%}{fabricated:>12}{slow:>6}{minutes:>7.1f}"
        )

    lines.append("-" * 78)
    broken = {
        label: sum(1 for r in results if r.infrastructure_error)
        for label, results in runs.items()
    }
    if any(broken.values()):
        detail = ", ".join(f"{label} {n}" for label, n in broken.items() if n)
        lines.append(
            f"WARNING: backend failures make this comparison unsound ({detail}) — "
            "those cases scored no model at all."
        )
    if ranked:
        ranked.sort(reverse=True)
        winner = ranked[0][4]
        lines.append(f"Best on tool accuracy, then grounding:  {winner}")
    lines.append("")
    return "\n".join(lines)


def per_case_matrix(runs: dict[str, list[CaseResult]]) -> str:
    """Which cases each model fails — where the differences actually are."""
    if not runs:
        return ""
    labels = list(runs)
    case_ids = [r.id for r in next(iter(runs.values()))]
    width = max(len("case"), *(len(c) for c in case_ids)) + 2

    lines = ["", f"{'case':<{width}}" + "".join(f"{l[:14]:<16}" for l in labels), "-" * 78]
    for index, case_id in enumerate(case_ids):
        row = f"{case_id:<{width}}"
        for label in labels:
            results = runs[label]
            mark = "pass" if index < len(results) and results[index].passed else "FAIL"
            row += f"{mark:<16}"
        lines.append(row)
    lines.append("")
    return "\n".join(lines)


def cache_path(directory: Path, model: str) -> Path:
    return directory / f"{model.replace(':', '_').replace('/', '_')}.json"


def save_run(directory: Path, model: str, results: list[CaseResult]) -> Path:
    """Persist one model's results so a comparison survives a shutdown.

    Three models is ~45 minutes of local inference. Holding all of it in memory
    until the last one finishes means closing the laptop costs the whole run.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = cache_path(directory, model)
    path.write_text(as_json(results, model))
    return path


def load_run(directory: Path, model: str, expected_cases: int) -> list[CaseResult] | None:
    """Reload a finished model, or None if it is absent or from a different suite."""
    path = cache_path(directory, model)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    cases = payload.get("cases", [])
    # A cached run from a different case list would silently compare models on
    # different questions, which is worse than re-running.
    if len(cases) != expected_cases:
        return None
    return [
        CaseResult(
            id=c["id"],
            passed=c["passed"],
            failures=c.get("failures", []),
            tools_used=c.get("tools", []),
            grounding_rate=c.get("grounding_rate", 1.0 if c["passed"] else 0.0),
            ungrounded=c.get("ungrounded", []),
            seconds=c.get("seconds", 0.0),
            answer=c.get("answer", ""),
            too_slow=c.get("too_slow", False),
            infrastructure_error=c.get("infrastructure_error", False),
        )
        for c in cases
    ]


def as_json(results: list[CaseResult], label: str) -> str:
    return json.dumps(
        {
            "model": label,
            "passed": sum(r.passed for r in results),
            "total": len(results),
            "grounding": sum(r.grounding_rate for r in results) / max(len(results), 1),
            "cases": [
                {
                    "id": r.id,
                    "passed": r.passed,
                    "failures": r.failures,
                    "tools": r.tools_used,
                    "ungrounded": r.ungrounded,
                    "seconds": round(r.seconds, 1),
                    "grounding_rate": r.grounding_rate,
                    "too_slow": r.too_slow,
                    "infrastructure_error": r.infrastructure_error,
                    # Only for failures, and only far enough to see what went
                    # wrong. "invented 78.5" is not diagnosable without the
                    # sentence it appeared in.
                    **({} if r.passed else {"answer": r.answer[:1500]}),
                }
                for r in results
            ],
        },
        indent=2,
    )
