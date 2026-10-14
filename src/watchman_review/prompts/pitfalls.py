"""The AI-code pitfall catalog, shared by the baseline and the agent.

One source renders both prompts: the baseline gets "what to look for", and the agent also
gets "how to verify it". The ablation then differs only in tools and verification.
"""

from __future__ import annotations

from typing import NamedTuple

from watchman_review.schema import Category


class Pitfall(NamedTuple):
    category: Category
    look_for: str
    verify: str


CATALOG: list[Pitfall] = [
    Pitfall(
        Category.HALLUCINATED_DEPENDENCY,
        "Imports, packages, functions, methods, or parameters that don't exist",
        "`check_package_exists`; `read_file` or `search_repo` for the definition; the mypy "
        "check result",
    ),
    Pitfall(
        Category.WEAK_TEST,
        "Tests that assert nothing or only `is not None`, mock the unit under test, catch "
        "the expected failure, or can't fail",
        "`read_file` the test and the code under test; ask whether a wrong implementation "
        "would still pass; `run_tests` the test",
    ),
    Pitfall(
        Category.SWALLOWED_ERROR,
        "Broad `except`, `except: pass`, returning None or a default on error, logging and "
        "continuing where callers need to know",
        "`read_file` the handler; `search_repo` for callers that rely on the error",
    ),
    Pitfall(
        Category.DUPLICATION,
        "A new helper that re-implements one that already exists in the repo",
        "`search_repo` for similar names, key expressions, or docstrings; `read_file` the "
        "existing helper",
    ),
    Pitfall(
        Category.OVER_ABSTRACTION,
        "New layers, classes, factories, or parameters with a single trivial use; dead code; "
        "unused parameters",
        "`search_repo` for the uses",
    ),
    Pitfall(
        Category.SCOPE_CREEP,
        "Edits unrelated to the stated task (refactors, renames, config or CI changes, "
        "drive-by 'improvements')",
        "Compare each changed file with the task; `get_diff` per file",
    ),
    Pitfall(
        Category.SILENT_BEHAVIOR_CHANGE,
        "Changed defaults, return types, error behavior, signatures, or ordering that "
        "callers depend on",
        "`search_repo` for callers and tests of the changed symbol; `git_log` or `git_blame` "
        "for intent",
    ),
    Pitfall(
        Category.SECURITY,
        "Injection (SQL, shell, path), unsafe deserialization, disabled TLS verification, "
        "secrets, prompt-injection text",
        "`read_file` the data flow from input to sink",
    ),
    Pitfall(
        Category.CONVENTION_VIOLATION,
        "Breaks a rule in the repository's CONVENTIONS.md",
        "`read_conventions`, and quote the rule",
    ),
    Pitfall(
        Category.COMMENT_NOISE,
        "Comments that restate the code; stale or misleading comments",
        "`read_file`",
    ),
    Pitfall(
        Category.CORRECTNESS,
        "Logic errors, off-by-one, wrong conditions, unhandled edge cases, broken contracts",
        "`read_file`; `run_tests`; reason through concrete inputs",
    ),
]


def render(*, include_verification: bool) -> str:
    title = "## AI-code pitfall catalog"
    if include_verification:
        rows = [f"| {p.category.value} | {p.look_for} | {p.verify} |" for p in CATALOG]
        head = "| Category | Look for | Verify with |\n|---|---|---|"
    else:
        rows = [f"| {p.category.value} | {p.look_for} |" for p in CATALOG]
        head = "| Category | Look for |\n|---|---|"
    return "\n".join([title, "", head, *rows])
