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
        lines.append(
            f"{r.id:<22}{mark:<8}{','.join(r.tools_used)[:32]:<34}{r.seconds:>6.1f}"
        )
        for failure in r.failures:
            lines.append(f"{'':22}  - {failure}")

    lines += [
        "-" * 74,
        f"passed             {passed}/{total}",
        f"correct tool use   {total - tool_failures}/{total}",
        f"grounding          {grounding:.1%}  ({fabrications} case(s) with invented numbers)",
        f"tokens             {tokens:,}",
        f"wall clock         {seconds:.0f}s   ($0.00 — runs on your machine)",
        "",
    ]
    return "\n".join(lines)


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
                }
                for r in results
            ],
        },
        indent=2,
    )
