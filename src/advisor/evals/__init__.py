"""Eval harness. Phase 6 — and, running locally, the thing that picks the model."""

from advisor.evals.runner import (
    CaseResult,
    as_json,
    check,
    load_cases,
    per_case_matrix,
    report,
    run_case,
    run_suite,
    scoreboard,
)

__all__ = [
    "CaseResult",
    "as_json",
    "check",
    "load_cases",
    "per_case_matrix",
    "report",
    "run_case",
    "run_suite",
    "scoreboard",
]
