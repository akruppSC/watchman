"""Review orchestration: diff -> snapshot -> layers -> post-processing -> ReviewResult."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from watchman_review.baseline import run_baseline
from watchman_review.budget import BudgetExceeded, Ledger, MeteredClient, ReviewBudget
from watchman_review.checks import CheckContext, run_checks
from watchman_review.config import (
    REPO_CONFIG_NAME,
    Config,
    ConfigError,
    data_dir,
    load_config,
)
from watchman_review.gitutil import WORKTREE, Diff, GitError, get_diff, run_git, snapshot
from watchman_review.llm import build_client
from watchman_review.llm.cache import CacheMode, CachingClient
from watchman_review.llm.types import LLMClient, LLMError
from watchman_review.postprocess import finalize
from watchman_review.sandbox import Sandbox, SandboxError, choose_sandbox
from watchman_review.schema import (
    CheckResult,
    CheckStatus,
    Finding,
    ReviewMode,
    ReviewResult,
    Usage,
    Verdict,
    compute_verdict,
)


class ReviewError(Exception):
    pass


@dataclass
class ReviewOptions:
    base: str = "main"
    head: str = "HEAD"
    mode: ReviewMode = ReviewMode.DETERMINISTIC
    task: str | None = None
    checks: list[str] | None = None
    offline: bool = False
    llm_cache: CacheMode = CacheMode.OFF
    llm_client: LLMClient | None = None  # injected by tests and the eval harness
    ledger: Ledger | None = field(default_factory=Ledger)
    cache_dir: Path | None = None
    sandbox: Sandbox | None = None


def read_at_rev(repo: Path, rev: str, path: str) -> str | None:
    try:
        return run_git(repo, "show", f"{rev}:{path}")
    except GitError:
        return None


def load_review_config(repo: Path, base: str) -> tuple[Config, list[str]]:
    """Config with the repo override taken from the base revision, not the change."""
    text = read_at_rev(repo, base, REPO_CONFIG_NAME)
    return load_config(repo, repo_config_text=text) if text is not None else load_config(None)


def load_conventions(repo: Path, base: str, tree: Path, name: str) -> tuple[str | None, str | None]:
    """Conventions come from base so a change can't relax its own rules. Returns (text, warning)."""
    text = read_at_rev(repo, base, name)
    if text is not None:
        return text, None
    head_path = tree / name
    if head_path.is_file():
        return head_path.read_text(
            errors="replace"
        ), f"{name} is new in this change; using head copy"
    return None, None


def _client(cfg: Config, opts: ReviewOptions, budget: ReviewBudget) -> LLMClient:
    inner = opts.llm_client or build_client(cfg.llm)
    if opts.llm_cache is not CacheMode.OFF:
        inner = CachingClient(inner, (opts.cache_dir or data_dir()) / "llm-cache", opts.llm_cache)
    return MeteredClient(inner, llm_cfg=cfg.llm, budget=budget, ledger=opts.ledger)


def run_review(repo: Path, opts: ReviewOptions, cfg: Config | None = None) -> ReviewResult:
    start = time.monotonic()
    warnings: list[str] = []
    if cfg is None:
        try:
            cfg, cfg_warnings = load_review_config(repo, opts.base)
        except ConfigError as e:
            raise ReviewError(str(e)) from e
        warnings += cfg_warnings

    try:
        diff = get_diff(repo, opts.base, opts.head)
    except GitError as e:
        raise ReviewError(str(e)) from e

    if not diff.files:
        return ReviewResult(
            mode=opts.mode, base=opts.base, head=opts.head, verdict=Verdict.PASS, findings=[],
            warnings=[*warnings, "empty diff: nothing to review"],
            latency_s=round(time.monotonic() - start, 3),
        )  # fmt: skip

    budget = ReviewBudget(cfg.budget)
    findings: list[Finding] = []
    checks: list[CheckResult] = []

    with snapshot(repo, opts.head) as tree:
        if opts.head == WORKTREE:
            warnings.append("reviewing the live working tree, not a commit snapshot")

        if opts.mode in (ReviewMode.DETERMINISTIC, ReviewMode.AGENT):
            checks = _run_layer1(repo, tree, diff, cfg, opts, warnings)
            findings += [f for c in checks for f in c.findings]

        if opts.mode is ReviewMode.BASELINE:
            conventions, conv_warning = load_conventions(
                repo, opts.base, tree, cfg.review.conventions_file
            )
            if conv_warning:
                warnings.append(conv_warning)
            try:
                client = _client(cfg, opts, budget)
                llm_findings, llm_warnings = run_baseline(
                    client, diff, task=opts.task, conventions=conventions,
                    max_tokens=cfg.llm.max_output_tokens, temperature=cfg.llm.temperature,
                    seed=cfg.llm.seed,
                )  # fmt: skip
            except (LLMError, BudgetExceeded, ConfigError) as e:
                raise ReviewError(f"LLM review failed: {e}") from e
            findings += llm_findings
            warnings += llm_warnings
        elif opts.mode is ReviewMode.AGENT:
            raise ReviewError("agent mode is not implemented yet (milestone 2)")

        final = finalize(findings, diff, tree, min_confidence=cfg.review.min_confidence)

    usage: Usage = budget.usage
    return ReviewResult(
        mode=opts.mode,
        base=opts.base,
        head=opts.head,
        verdict=compute_verdict(
            final.findings, block_min_confidence=cfg.review.block_min_confidence
        ),
        findings=final.findings,
        checks=checks,
        usage=usage,
        latency_s=round(time.monotonic() - start, 3),
        dropped_findings=len(final.dropped),
        warnings=warnings,
    )


def _run_layer1(
    repo: Path, tree: Path, diff: Diff, cfg: Config, opts: ReviewOptions, warnings: list[str]
) -> list[CheckResult]:
    names = opts.checks if opts.checks is not None else cfg.review.checks
    sandbox = opts.sandbox
    if sandbox is None and "pytest" in names:
        try:
            sandbox, sb_warnings = choose_sandbox(cfg.review.sandbox, cfg.review.docker_image)
            warnings += sb_warnings
        except SandboxError as e:
            warnings.append(f"tests skipped: {e}")
    ctx = CheckContext(
        repo_root=repo, tree=tree, diff=diff, config=cfg, sandbox=sandbox, task=opts.task,
        offline=opts.offline, cache_dir=opts.cache_dir or data_dir(),
    )  # fmt: skip
    try:
        results = run_checks(ctx, names)
    except ValueError as e:
        raise ReviewError(str(e)) from e
    for r in results:
        if r.status is CheckStatus.ERROR:
            warnings.append(f"check {r.name} errored: {r.summary}")
    return results
