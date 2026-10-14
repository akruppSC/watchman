"""Single-prompt baseline reviewer: "review this diff". No tools and no Layer 1 evidence.

This is the control arm for the ablation. It gets the same pitfall vocabulary and output
schema as the agent, so the comparison measures tools and verification, not prompt wording.
"""

from __future__ import annotations

from watchman_review import prompts
from watchman_review.gitutil import Diff
from watchman_review.llm.types import CompletionRequest, LLMClient, Message
from watchman_review.parsing import FindingsParseError, parse_findings
from watchman_review.prompts import pitfalls
from watchman_review.schema import Finding, Source

MAX_DIFF_CHARS = 120_000


def build_messages(diff: Diff, task: str | None, conventions: str | None) -> list[Message]:
    system = "\n\n".join(
        [
            prompts.load("baseline_system"),
            pitfalls.render(include_verification=False),
            prompts.load("severity_guide"),
            prompts.load("findings_format"),
        ]
    )
    raw = diff.raw
    truncated = len(raw) > MAX_DIFF_CHARS
    if truncated:
        raw = raw[:MAX_DIFF_CHARS]
    parts: list[str] = []
    if task:
        parts.append("Stated task for this change:\n" + prompts.wrap_untrusted("task", task))
    if conventions:
        parts.append(
            "Repository conventions (CONVENTIONS.md):\n"
            + prompts.wrap_untrusted("conventions", conventions)
        )
    parts.append(
        "Diff to review"
        + (" (truncated)" if truncated else "")
        + ":\n"
        + prompts.wrap_untrusted("diff", raw)
    )
    return [Message.system(system), Message.user("\n\n".join(parts))]


def run_baseline(
    client: LLMClient,
    diff: Diff,
    *,
    task: str | None = None,
    conventions: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    seed: int | None = None,
) -> tuple[list[Finding], list[str], bool]:
    """Returns (findings, warnings, complete). complete is False if the output was unusable."""
    req = CompletionRequest(
        messages=build_messages(diff, task, conventions),
        max_tokens=max_tokens,
        temperature=temperature,
        seed=seed,
    )
    resp = client.complete(req)
    try:
        findings, errors = parse_findings(resp.content, Source.BASELINE)
    except FindingsParseError as e:
        return [], [f"baseline output unparseable: {e}"], False
    return findings, [f"baseline dropped invalid {err}" for err in errors], True
