"""Checkpoint control for live mode.

A checkpoint is a moment worth reviewing: the worker ran tests (`test_run`), made a commit
(`commit`), or says it is finished (`done`). Each kind has its own review/fix episode:

  round 1: review a snapshot -> findings shown to the worker
  round 2: the worker fixed things -> review again; findings already shown collapse
  round 3 (the cap): anything still open escalates to the human instead of a 4th round

Policy is advisory by default (feedback only). With `blocking`, a BLOCK verdict stops the
commit or "done" until it is fixed or the cap is hit, and the cap always releases the gate so
a stubborn false positive can never trap the worker.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from watchman_review.config import Config, LivePolicy
from watchman_review.gitutil import GitError
from watchman_review.live import state as st
from watchman_review.live.dedupe import finding_key
from watchman_review.live.feedback import Feedback, LiveFinding
from watchman_review.live.snapshot import _git, head_sha, snapshot_commit
from watchman_review.review import ReviewError, ReviewOptions, load_review_config, run_review
from watchman_review.schema import Finding, ReviewMode, ReviewResult, Severity, Verdict

KINDS = ("test_run", "commit", "done")
ReviewFn = Callable[[Path, ReviewOptions, Config | None], ReviewResult]


class LiveError(Exception):
    pass


def _default_base(repo: Path) -> str | None:
    """The default branch tip, if HEAD is not itself that commit."""
    head = head_sha(repo)
    for ref in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        try:
            sha = _git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
        except GitError:
            continue
        if sha != head:
            return sha
    return None


def start(
    repo: Path, session: str, *, task: str | None = None, base: str | None = None
) -> st.SessionState:
    """Record where the worker started, so later commits don't shrink the reviewed diff."""
    with st.locked(repo, session) as state:
        if base:
            state.base = _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}")
        elif state.base is None:
            state.base = head_sha(repo)
        if task:
            state.task = task
        return state.model_copy(deep=True)


def write_inbox(repo: Path, session: str, fb: Feedback) -> Path:
    """Channel 3: a plain file for agents with no hooks or MCP. Always the latest feedback."""
    path = repo / st.STATE_DIR.parent / "inbox.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<!-- session {session}, {st.now()} -->\n{fb.render()}\n")
    return path


def reset(repo: Path, session: str, kind: str | None = None) -> None:
    with st.locked(repo, session) as state:
        if kind is None:
            state.episodes.clear()
        else:
            state.episodes.pop(kind, None)


def _reader(repo: Path, sha: str) -> Callable[[str, int], str | None]:
    cache: dict[str, list[str]] = {}

    def read(path: str, line: int) -> str | None:
        if path not in cache:
            try:
                cache[path] = _git(repo, "show", f"{sha}:{path}").splitlines()
            except GitError:
                cache[path] = []
        lines = cache[path]
        return lines[line - 1] if 0 < line <= len(lines) else None

    return read


def _live(f: Finding, key: str, state: str, since: int) -> LiveFinding:
    loc = f.file or "(change)"
    if f.file and f.line_start:
        loc += f":{f.line_start}"
    return LiveFinding(
        key=key,
        state="new" if state == "new" else "open",
        severity=f.severity.value,
        category=f.category.value,
        location=loc,
        summary=f.summary[:110],
        fix=(f.suggested_fix or "")[:90] or None,
        ref=f.evidence_refs[0] if f.evidence_refs else None,
        since_round=since,
    )


def _mode_for(kind: str, cfg: Config) -> ReviewMode:
    return {
        "test_run": cfg.live.test_run_mode,
        "commit": cfg.live.commit_mode,
        "done": cfg.live.done_mode,
    }[kind]


def _write_escalation(repo: Path, session: str, kind: str, fb: Feedback) -> None:
    path = repo / st.STATE_DIR / f"ESCALATION-{session}-{kind}.md"
    items = [*fb.new, *fb.open]
    path.write_text(
        f"# Watchman escalation ({kind}, session {session})\n\n"
        f"These findings were still open after {fb.cap} review/fix rounds. Watchman stopped "
        "asking the worker to fix them and is handing the decision to you.\n\n"
        + "\n".join(f"- **{f.severity}** {f.category} `{f.location}`: {f.summary}" for f in items)
        + "\n\nRun `watchman live reset` to start the loop over.\n"
    )


def check(
    repo: Path,
    *,
    kind: str,
    session: str = st.DEFAULT_SESSION,
    task: str | None = None,
    base: str | None = None,
    policy: LivePolicy | None = None,
    cfg: Config | None = None,
    review_fn: ReviewFn = run_review,
    options: ReviewOptions | None = None,
) -> Feedback:
    """Review a snapshot of the worker's tree at a checkpoint and return terse feedback."""
    if kind not in KINDS:
        raise LiveError(f"unknown checkpoint kind {kind!r}; use one of {', '.join(KINDS)}")
    with st.locked(repo, session) as state:
        if task:
            state.task = task
        if base:
            state.base = _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}")
        if state.base is None:
            state.base = _default_base(repo) or head_sha(repo)
        # The repo's own .watchman.toml comes from the base revision, never from the worker's
        # working tree: a worker must not be able to loosen its own review.
        cfg = cfg or load_review_config(repo, state.base or "HEAD")[0]
        pol = policy or cfg.live.policy
        cap = cfg.live.round_cap
        ep = state.episode(kind)

        def done(fb: Feedback) -> Feedback:
            fb.kind, fb.cap, fb.policy, fb.base = kind, cap, pol.value, state.base or ""
            return fb

        if ep.status == "escalated" and ep.last_feedback:
            fb = Feedback.model_validate(ep.last_feedback)
            fb.message = "Already escalated to the human; no further rounds."
            return done(fb.model_copy(update={"gate": "released", "status": "escalated"}))

        snap = snapshot_commit(repo)
        if snap == state.base:
            return done(Feedback(kind=kind, status="nothing", snapshot=snap))
        if snap == ep.last_snapshot and ep.last_feedback:
            fb = Feedback.model_validate(ep.last_feedback)
            return done(fb.model_copy(update={"status": "unchanged", "cost_usd": 0.0}))

        mode = _mode_for(kind, cfg)
        opts = options or ReviewOptions()
        opts.base, opts.head, opts.mode = state.base or "HEAD", snap, mode
        opts.task = state.task
        if kind == "test_run":
            opts.checks = list(cfg.live.test_run_checks)
        try:
            result = review_fn(repo, opts, cfg)
        except ReviewError as e:
            return done(Feedback(kind=kind, status="error", snapshot=snap, message=str(e)[:160]))
        if result.incomplete:
            reasons = "; ".join(result.incomplete_reasons) or "a model call failed"
            return done(Feedback(kind=kind, status="error", snapshot=snap, message=reasons[:160]))

        read = _reader(repo, snap)
        shown = [f for f in result.findings if f.severity is not Severity.NIT]
        nits = len(result.findings) - len(shown)
        order = sorted(shown, key=lambda f: (-f.severity.rank, f.file or "", f.line_start))
        keyed = [(finding_key(f, read), f) for f in order]
        current = {k for k, _ in keyed}
        # "Fixed" is judged within this checkpoint's own review scope (a cheap test-run review
        # can't see what an agent review found). "Already shown" is judged across all of the
        # session's checkpoints, so a problem flagged at the commit isn't announced as new again
        # when the worker says it is done.
        fixed = [k for k in ep.seen if k not in current]
        known = {k: v for other in state.episodes.values() for k, v in other.seen.items()}
        known.update(ep.seen)
        new_items = [(k, f) for k, f in keyed if k not in known]
        open_items = [(k, f) for k, f in keyed if k in known]

        round_no = ep.round + 1 if ep.status == "open" else 1
        has_open = bool(keyed)
        fb = Feedback(
            kind=kind, round=round_no if has_open else ep.round, verdict=result.verdict.value,
            status="open" if has_open else "clean", fixed=len(fixed), nits=nits, snapshot=snap,
            cost_usd=result.usage.cost_usd,
        )  # fmt: skip
        budget = cfg.live.max_findings_shown
        fb.new = [_live(f, k, "new", round_no) for k, f in new_items][:budget]
        room = max(0, budget - len(fb.new))
        fb.open = [_live(f, k, "open", known[k].round) for k, f in open_items][:room]
        fb.hidden = len(keyed) - len(fb.new) - len(fb.open)

        if not has_open:
            ep.status, ep.round, ep.seen = "clean", 0, {}
            fb.gate = "pass"
        else:
            ep.status, ep.round = "open", round_no
            for k in fixed:
                ep.seen.pop(k, None)
            for k, _ in open_items:
                ep.seen.setdefault(k, known[k])
            for k, f in new_items:
                loc = f"{f.file}:{f.line_start}" if f.file else "(change)"
                ep.seen[k] = st.Seen(
                    round=round_no, severity=f.severity.value, category=f.category.value,
                    location=loc, summary=f.summary[:110],
                )  # fmt: skip
            blocking = pol is LivePolicy.BLOCKING and result.verdict is Verdict.BLOCK
            fb.gate = "block" if blocking else "pass"
            if round_no >= cap:
                ep.status, fb.status, fb.gate = "escalated", "escalated", "released"
                _write_escalation(repo, session, kind, fb)
        ep.last_snapshot, ep.last_feedback = snap, fb.model_dump(mode="json")
        state.history.append(
            st.HistoryEntry(
                ts=st.now(),
                kind=kind,
                round=fb.round,
                snapshot=snap,
                verdict=fb.verdict,
                new=len(new_items),
                open=len(open_items),
                fixed=len(fixed),
                cost_usd=fb.cost_usd,
                gate=fb.gate,
            )
        )
        return done(fb)
