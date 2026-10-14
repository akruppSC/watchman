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
    BudgetConfig,
    Config,
    ConfigError,
    OverseerTrigger,
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
from watchman_review.overseer import Supervision, risk_reasons, should_run, supervise
from watchman_review.postprocess import finalize, ground, threshold
from watchman_review.pypi import PackageIndex
from watchman_review.sandbox import Sandbox, SandboxError, choose_sandbox
from watchman_review.schema import (
    CheckResult,
    CheckStatus,
    DroppedFinding,
    Finding,
    ReviewMode,
    ReviewResult,
    Source,
    StageSummary,
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
    # None -> follow [overseer] in the user config. "always"/"risky" switch the overseer on
    # even if the config has it off (evals, --deep); "never" switches it off.
    overseer: OverseerTrigger | None = None
    overseer_client: LLMClient | None = None  # injected by tests


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


def effective_trigger(cfg: Config, opts: ReviewOptions, mode: ReviewMode) -> OverseerTrigger:
    if mode not in (ReviewMode.AGENT, ReviewMode.AGENT_NO_L1):
        return OverseerTrigger.NEVER
    if opts.overseer is not None:
        return opts.overseer
    return cfg.overseer.trigger if cfg.overseer.auto else OverseerTrigger.NEVER


def _stage_budget(
    cfg: Config, *, tool_calls: int, max_cost: float, max_tokens: int
) -> ReviewBudget:
    return ReviewBudget(
        BudgetConfig(
            max_tool_calls=tool_calls,
            max_tokens_per_review=max_tokens,
            max_cost_per_review_usd=max_cost,
            monthly_cap_usd=cfg.budget.monthly_cap_usd,
        )
    )


def _overseer_client(cfg: Config, opts: ReviewOptions, budget: ReviewBudget) -> LLMClient:
    oc = cfg.overseer
    llm = cfg.llm.model_copy(
        update={
            "model": oc.model,
            "input_cost_per_mtok": oc.input_cost_per_mtok,
            "output_cost_per_mtok": oc.output_cost_per_mtok,
        }
    )
    inner = opts.overseer_client or build_client(llm)
    if opts.llm_cache is not CacheMode.OFF:
        inner = CachingClient(inner, (opts.cache_dir or data_dir()) / "llm-cache", opts.llm_cache)
    return MeteredClient(inner, llm_cfg=llm, budget=budget, ledger=opts.ledger, purpose="overseer")


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
    stages: list[StageSummary] = []
    sup: Supervision | None = None
    primary_usage: Usage | None = None
    trigger = effective_trigger(cfg, opts, mode)
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
                    tctx = _tool_context(ctx, conventions, cfg)
                    layer1 = checks if mode is ReviewMode.AGENT else None
                    outcome = run_agent(
                        client, tctx, checks=layer1, budget=budget,
                        max_tokens=cfg.llm.max_output_tokens, temperature=cfg.llm.temperature,
                        seed=cfg.llm.seed, escalations=trigger is not OverseerTrigger.NEVER,
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
                    primary_usage = budget.usage.model_copy(deep=True)
                    stages.append(
                        StageSummary(
                            name="primary",
                            model=client.model,
                            usage=primary_usage,
                            escalations=outcome.escalations,
                        )
                    )
                    if trigger is not OverseerTrigger.NEVER:
                        sup = _supervise(
                            repo=repo, tree=tree, diff=diff, cfg=cfg, opts=opts, trigger=trigger,
                            tctx=tctx, layer1=layer1, outcome=outcome, prior=findings,
                            stages=stages, warnings=warnings,
                        )  # fmt: skip
                        if sup is not None:
                            llm_findings = sup.findings
                            trace = [*trace, *sup.trace]
                            llm_warnings = [*llm_warnings, *sup.warnings]
            except (LLMError, BudgetExceeded, ConfigError) as e:
                raise ReviewError(f"LLM review failed: {e}") from e
            findings += llm_findings
            warnings += llm_warnings
            if not complete:
                incomplete_reasons.append(f"{mode.value} model stage did not finish")

        final = finalize(findings, diff, tree, min_confidence=cfg.review.min_confidence)

    usage: Usage = budget.usage.model_copy(deep=True)
    extra_dropped: list[tuple[Finding, str]] = []
    if sup is not None:
        usage.add(sup.usage)
        extra_dropped = sup.dropped
        stages.extend(sup.stages)
    all_dropped = final.dropped + extra_dropped
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
        dropped_findings=len(all_dropped),
        dropped=[
            DroppedFinding(finding=f, reason=f"{r}; {grading[f.id]}" if f.id in grading else r)
            for f, r in all_dropped
        ],
        stages=stages,
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


def _tool_context(ctx: CheckContext, conventions: str | None, cfg: Config) -> ToolContext:
    return ToolContext(
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


def _supervise(
    *, repo: Path, tree: Path, diff: Diff, cfg: Config, opts: ReviewOptions,
    trigger: OverseerTrigger, tctx: ToolContext, layer1: list[CheckResult] | None,
    outcome: AgentOutcome, prior: list[Finding], stages: list[StageSummary], warnings: list[str],
) -> Supervision | None:  # fmt: skip
    """Decide whether to call the overseer and, if so, run it. Never fails the review."""
    oc = cfg.overseer
    pre = threshold(ground(outcome.findings, diff, tree).kept, cfg.review.min_confidence).kept
    reportable = [f for f in pre if f.source is Source.AGENT]
    reasons = risk_reasons(
        primary=outcome, reportable=reportable, checks=layer1, diff=diff, oc=oc
    )  # fmt: skip
    primary_stage = stages[-1]
    primary_stage.trigger_reasons = reasons
    # Paired baseline for evals: what this review would report without the overseer.
    primary_stage.findings_before_overseer = finalize(
        [*prior, *outcome.findings], diff, tree, min_confidence=cfg.review.min_confidence
    ).findings
    if not should_run(trigger, reasons):
        stages.append(StageSummary(name="overseer", model=oc.model, ran=False))
        return None
    ob = _stage_budget(
        cfg, tool_calls=oc.max_tool_calls, max_cost=oc.max_cost_usd, max_tokens=200_000
    )
    try:
        return supervise(
            cfg=cfg, tctx=tctx, checks=layer1, primary=outcome, reportable=reportable,
            reasons=reasons, overseer_client=_overseer_client(cfg, opts, ob), overseer_budget=ob,
            make_second_pass=lambda: _second_pass(cfg, opts),
        )  # fmt: skip
    except (LLMError, BudgetExceeded, ConfigError) as e:
        msg = f"overseer unavailable: {e}"
        warnings.append(f"{msg}; the primary reviewer's findings were not independently verified")
        stages.append(StageSummary(name="overseer", model=oc.model, ran=False, error=msg))
        return None


def _second_pass(cfg: Config, opts: ReviewOptions) -> tuple[LLMClient, ReviewBudget]:
    budget = _stage_budget(
        cfg, tool_calls=cfg.overseer.second_pass_tool_calls,
        max_cost=cfg.budget.max_cost_per_review_usd, max_tokens=cfg.budget.max_tokens_per_review,
    )  # fmt: skip
    return _client(cfg, opts, budget), budget
