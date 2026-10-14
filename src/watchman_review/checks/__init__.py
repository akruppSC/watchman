"""Layer 1: deterministic checks (cheap, high precision, run first)."""

from __future__ import annotations

from watchman_review.checks.base import Check, CheckContext, run_check
from watchman_review.checks.hygiene import HygieneCheck
from watchman_review.checks.imports import ImportsCheck
from watchman_review.checks.mypy_check import MypyCheck
from watchman_review.checks.ruff_check import RuffCheck
from watchman_review.checks.scope import ScopeCheck
from watchman_review.checks.secrets import SecretsCheck
from watchman_review.checks.tests_check import TestsCheck
from watchman_review.schema import CheckResult

REGISTRY: dict[str, type[Check]] = {
    c.name: c
    for c in (
        RuffCheck,
        MypyCheck,
        TestsCheck,
        SecretsCheck,
        ImportsCheck,
        ScopeCheck,
        HygieneCheck,
    )
}


class UnknownCheck(ValueError):
    pass


def run_checks(ctx: CheckContext, names: list[str]) -> list[CheckResult]:
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        raise UnknownCheck(f"unknown check(s): {unknown}; available: {sorted(REGISTRY)}")
    return [run_check(REGISTRY[n](), ctx) for n in names]


__all__ = ["REGISTRY", "Check", "CheckContext", "run_check", "run_checks"]
