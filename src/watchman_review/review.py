"""Review orchestration: diff -> snapshot -> layers -> post-processing -> ReviewResult."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from watchman_review.agent import AgentOutcome, ToolContext, run_agent
from watchman_review.baseline import run_baseline
from watchman_review.budget import BudgetExceeded, Ledger, MeteredClient, ReviewBudget
from watchman_review.checks import CheckContext, run_checks
from watchman_review.config import (
    REPO_CONFIG_NAME,
    Config,
    ConfigError,
    Provider,
    data_dir,
    load_config,
)
from watchman_review.gitutil import (
    WORKTREE,
    Diff,
    GitError,
    get_diff,
    rev_parse,
    run_git,
    snapshot,
)
from watchman_review.llm import build_client
from watchman_review.llm.cache import CacheMode, CachingClient
from watchman_review.llm.types import LLMClient, LLMError
from watchman_review.postprocess import finalize
from watchman_review.pypi import PackageIndex
from watchman_review.sandbox import Sandbox, SandboxError, choose_sandbox
from watchman_review.schema import (
    CheckResult,
    CheckStatus,
    DroppedFinding,
    Finding,
    ReviewMode,
    ReviewResult,
    ToolTraceStep,
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
    mode: ReviewMode | None = None  # None -> config review.default_mode
    task: str | None = None
    model: str | None = None  # override the configured LLM model (evals, one-off runs)
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


def _apply_overrides(cfg: Config, opts: ReviewOptions, warnings: list[str]) -> Config:
    if not opts.model or opts.model == cfg.llm.model:
        return cfg
    cfg = cfg.model_copy(deep=True)
    cfg.llm.model = opts.model
    # Configured prices belong to the configured model. OpenRouter reports real per-call
    # cost, so tracking still works there; elsewhere, dollar budgets can't be enforced.
    cfg.llm.input_cost_per_mtok = cfg.llm.output_cost_per_mtok = None
    if cfg.llm.provider is not Provider.OPENROUTER:
        warnings.append(f"model override {opts.model!r}: pricing unknown, cost not tracked")
    return cfg


def run_review(repo: Path, opts: ReviewOptions, cfg: Config | None = None) -> ReviewResult:
    start = time.monotonic()
    warnings: list[str] = []
    if cfg is None:
        try:
            cfg, cfg_warnings = load_review_config(repo, opts.base)
        except ConfigError as e:
            raise ReviewError(str(e)) from e
        warnings += cfg_warnings
    cfg = _apply_overrides(cfg, opts, warnings)
    mode = opts.mode or cfg.review.default_mode

    try:
        diff = get_diff(repo, opts.base, opts.head)
    except GitError as e:
        raise ReviewError(str(e)) from e

    if not diff.files:
        return ReviewResult(
            mode=mode, base=opts.base, head=opts.head, verdict=Verdict.PASS, findings=[],
            warnings=[*warnings, "empty diff: nothing to review"],
            latency_s=round(time.monotonic() - start, 3),
        )  # fmt: skip

    budget = ReviewBudget(cfg.budget)
    findings: list[Finding] = []
    checks: list[CheckResult] = []
    trace: list[ToolTraceStep] = []
    grading: dict[str, str] = {}
    incomplete_reasons: list[str] = []
    runs_layer1 = mode in (ReviewMode.DETERMINISTIC, ReviewMode.AGENT)
    uses_agent = mode in (ReviewMode.AGENT, ReviewMode.AGENT_NO_L1)

    with snapshot(repo, opts.head) as tree:
        if opts.head == WORKTREE:
            warnings.append("reviewing the live working tree, not a commit snapshot")

        names = opts.checks if opts.checks is not None else cfg.review.checks
        needs_sandbox = (runs_layer1 and "pytest" in names) or uses_agent
        ctx = _check_context(repo, tree, diff, cfg, opts, warnings, needs_sandbox)

        if runs_layer1:
            checks = _run_layer1(ctx, names, warnings)
            findings += [f for c in checks for f in c.findings]

        if mode is not ReviewMode.DETERMINISTIC:
            conventions, conv_warning = load_conventions(
                repo, opts.base, tree, cfg.review.conventions_file
            )
            if conv_warning:
                warnings.append(conv_warning)
            try:
                client = _client(cfg, opts, budget)
                if mode is ReviewMode.BASELINE:
                    llm_findings, llm_warnings, complete = run_baseline(
                        client, diff, task=opts.task, conventions=conventions,
                        max_tokens=cfg.llm.max_output_tokens, temperature=cfg.llm.temperature,
                        seed=cfg.llm.seed,
                    )  # fmt: skip
                else:
                    outcome = _run_agent(
                        client, ctx, conventions, budget, cfg,
                        checks if mode is ReviewMode.AGENT else None,
                    )  # fmt: skip
                    complete = not outcome.incomplete
                    llm_findings, llm_warnings, trace = (
                        outcome.findings, outcome.warnings, outcome.trace
                    )  # fmt: skip
                    grading = {
                        g.finding_id: f"grounding={g.grade.value}"
                        + (f", invalid refs {g.invalid_refs}" if g.invalid_refs else "")
                        for g in outcome.grounding
                    }
            except (LLMError, BudgetExceeded, ConfigError) as e:
                raise ReviewError(f"LLM review failed: {e}") from e
            findings += llm_findings
            warnings += llm_warnings
            if not complete:
                incomplete_reasons.append(f"{mode.value} model stage did not finish")

        final = finalize(findings, diff, tree, min_confidence=cfg.review.min_confidence)

    usage: Usage = budget.usage
    verdict = compute_verdict(final.findings, block_min_confidence=cfg.review.block_min_confidence)
    if incomplete_reasons and verdict is Verdict.PASS:
        verdict = Verdict.WARN  # an unfinished review must never look clean
    return ReviewResult(
        mode=mode,
        base=opts.base,
        head=opts.head,
        verdict=verdict,
        findings=final.findings,
        checks=checks,
        usage=usage,
        latency_s=round(time.monotonic() - start, 3),
        dropped_findings=len(final.dropped),
        dropped=[
            DroppedFinding(finding=f, reason=f"{r}; {grading[f.id]}" if f.id in grading else r)
            for f, r in final.dropped
        ],
        warnings=warnings,
        tool_trace=trace,
        incomplete=bool(incomplete_reasons),
        incomplete_reasons=incomplete_reasons,
    )


def _check_context(
    repo: Path, tree: Path, diff: Diff, cfg: Config, opts: ReviewOptions, warnings: list[str],
    needs_sandbox: bool,
) -> CheckContext:  # fmt: skip
    sandbox = opts.sandbox
    if sandbox is None and needs_sandbox:
        try:
            sandbox, sb_warnings = choose_sandbox(cfg.review.sandbox, cfg.review.docker_image)
            warnings += sb_warnings
        except SandboxError as e:
            warnings.append(f"tests unavailable: {e}")
    return CheckContext(
        repo_root=repo, tree=tree, diff=diff, config=cfg, sandbox=sandbox, task=opts.task,
        offline=opts.offline, cache_dir=opts.cache_dir or data_dir(),
    )  # fmt: skip


def _run_layer1(ctx: CheckContext, names: list[str], warnings: list[str]) -> list[CheckResult]:
    try:
        results = run_checks(ctx, names)
    except ValueError as e:
        raise ReviewError(str(e)) from e
    for r in results:
        if r.status is CheckStatus.ERROR:
            warnings.append(f"check {r.name} errored: {r.summary}")
    return results


def _run_agent(
    client: LLMClient, ctx: CheckContext, conventions: str | None, budget: ReviewBudget,
    cfg: Config, checks: list[CheckResult] | None,
) -> AgentOutcome:  # fmt: skip
    tctx = ToolContext(
        repo_root=ctx.repo_root,
        tree=ctx.tree,
        diff=ctx.diff,
        config=cfg,
        head_rev=None if ctx.diff.head == WORKTREE else rev_parse(ctx.repo_root, ctx.diff.head),
        sandbox=ctx.sandbox,
        index=PackageIndex(http=ctx.http, cache_dir=ctx.cache_dir, offline=ctx.offline),
        conventions=conventions,
        task=ctx.task,
        check_ctx=ctx,
    )
    return run_agent(
        client, tctx, checks=checks, budget=budget, max_tokens=cfg.llm.max_output_tokens,
        temperature=cfg.llm.temperature, seed=cfg.llm.seed,
    )  # fmt: skip
