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

# Phrases that indicate the model committed to a choice.
PICK_MARKERS = (
    "start", "i'd", "i would", "recommend", "go with", "pick", "choose",
    "better", "accept", "decline", "yes", "no", "hold", "trade", "keep",
)


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

    if case.get("wants_pick") and not any(m in lowered for m in PICK_MARKERS):
        result.failures.append("no recommendation given")

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


def run_case(backend: Backend, ctx: LeagueContext, case: dict) -> CaseResult:
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

    return check(case, turn, messages)


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
