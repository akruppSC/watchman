"""Command-line interface."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from watchman_review import __version__
from watchman_review import hook as hooks
from watchman_review.budget import Ledger
from watchman_review.ci import CiError, CiOptions, run_ci
from watchman_review.config import (
    ConfigError,
    LivePolicy,
    OverseerTrigger,
    Provider,
    load_config,
    save_user_config,
    user_config_path,
)
from watchman_review.gitutil import WORKTREE, GitError, repo_root
from watchman_review.llm.cache import CacheMode
from watchman_review.report import render
from watchman_review.report_md import render_markdown
from watchman_review.review import ReviewError, ReviewOptions, run_review
from watchman_review.schema import ReviewMode, ReviewResult, Verdict

app = typer.Typer(
    name="watchman",
    help="AI reviewer for code written by AI coding agents.",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(help="Inspect configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")
hook_app = typer.Typer(help="Manage the git pre-push hook.", no_args_is_help=True)
app.add_typer(hook_app, name="hook")
live_app = typer.Typer(
    help="Live mode: supervise a coding agent at checkpoints.", no_args_is_help=True
)
app.add_typer(live_app, name="live")

err = Console(stderr=True)
out = Console()


class FailOn(StrEnum):
    BLOCK = "block"
    WARN = "warn"
    NEVER = "never"


EXIT_OK, EXIT_GATE, EXIT_ERROR = 0, 1, 2


def gate_exit_code(verdict: Verdict, fail_on: FailOn) -> int:
    if fail_on is FailOn.NEVER:
        return EXIT_OK
    if verdict is Verdict.BLOCK:
        return EXIT_GATE
    if verdict is Verdict.WARN and fail_on is FailOn.WARN:
        return EXIT_GATE
    return EXIT_OK


@app.callback()
def main() -> None:
    """AI reviewer for code written by AI coding agents."""


@app.command()
def version() -> None:
    """Print the Watchman version."""
    typer.echo(__version__)


@app.command()
def review(
    base: Annotated[
        str, typer.Option(help="Base ref; the diff starts at merge-base(base, head).")
    ] = "main",
    head: Annotated[str, typer.Option(help="Head ref to review.")] = "HEAD",
    worktree: Annotated[
        bool, typer.Option("--worktree", help="Review uncommitted working-tree changes.")
    ] = False,
    mode: Annotated[
        ReviewMode | None,
        typer.Option(help="Which reviewer to run. Default: review.default_mode in user config."),
    ] = None,
    model: Annotated[
        str | None, typer.Option(help="Override the configured LLM model for this run.")
    ] = None,
    task: Annotated[str | None, typer.Option(help="What the change was supposed to do.")] = None,
    task_file: Annotated[
        Path | None, typer.Option(help="Read the task description from a file.")
    ] = None,
    checks: Annotated[
        str | None, typer.Option(help="Comma-separated deterministic checks to run.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON to stdout.")] = False,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Also write JSON here.")
    ] = None,
    markdown: Annotated[
        Path | None, typer.Option("--markdown", help="Also write a Markdown report here.")
    ] = None,
    fail_on: Annotated[
        FailOn, typer.Option(help="Exit 1 when the verdict reaches this level.")
    ] = FailOn.BLOCK,
    deep: Annotated[
        bool, typer.Option("--deep", help="Always run the overseer (larger model) on this review.")
    ] = False,
    no_overseer: Annotated[
        bool, typer.Option("--no-overseer", help="Skip the overseer even if it is enabled.")
    ] = False,
    offline: Annotated[bool, typer.Option(help="No PyPI lookups.")] = False,
    llm_cache: Annotated[CacheMode, typer.Option(help="LLM record/replay cache.")] = CacheMode.OFF,
    repo: Annotated[Path, typer.Option(help="Repository path.")] = Path("."),
) -> None:
    """Review a git diff and emit structured findings."""
    try:
        root = repo_root(repo.resolve())
    except GitError as e:
        err.print(f"[red]error:[/red] not a git repository: {e}")
        raise typer.Exit(EXIT_ERROR) from e

    if deep and no_overseer:
        err.print("[red]error:[/red] --deep and --no-overseer contradict each other")
        raise typer.Exit(EXIT_ERROR)
    if task_file is not None:
        task = task_file.read_text()
    opts = ReviewOptions(
        overseer=OverseerTrigger.ALWAYS if deep else OverseerTrigger.NEVER if no_overseer else None,
        base=base,
        head=WORKTREE if worktree else head,
        mode=mode,
        model=model,
        task=task,
        checks=[c.strip() for c in checks.split(",") if c.strip()] if checks else None,
        offline=offline,
        llm_cache=llm_cache,
    )
    try:
        with err.status("reviewing...", spinner="dots"):
            result = run_review(root, opts)
    except ReviewError as e:
        err.print(f"[red]error:[/red] {e}")
        raise typer.Exit(EXIT_ERROR) from e

    payload = result.model_dump_json(indent=2)
    if output is not None:
        output.write_text(payload + "\n")
    if markdown is not None:
        markdown.write_text(render_markdown(result) + "\n")
    if as_json:
        typer.echo(payload)
    else:
        render(result, out)
    if result.incomplete:
        err.print(
            "[red]review incomplete:[/red] " + "; ".join(result.incomplete_reasons)
            + ". Findings may be missing; exiting 2 so this is never mistaken for a pass."
        )  # fmt: skip
        raise typer.Exit(EXIT_ERROR)
    raise typer.Exit(gate_exit_code(result.verdict, fail_on))


@app.command("ci")
def ci_command(
    mode: Annotated[ReviewMode, typer.Option(help="Reviewer to run.")] = ReviewMode.DETERMINISTIC,
    model: Annotated[str | None, typer.Option(help="Override the LLM model.")] = None,
    provider: Annotated[
        Provider | None, typer.Option(help="LLM provider (default: from config).")
    ] = None,
    checks: Annotated[str | None, typer.Option(help="Comma-separated Layer 1 checks.")] = None,
    base: Annotated[str | None, typer.Option(help="Base ref (default: the PR's base).")] = None,
    head: Annotated[str | None, typer.Option(help="Head ref (default: the PR's head).")] = None,
    deep: Annotated[bool, typer.Option("--deep", help="Run the overseer too.")] = False,
    comment: Annotated[
        bool, typer.Option("--comment/--no-comment", help="Post or update the PR comment.")
    ] = True,
    fail_on: Annotated[
        FailOn, typer.Option(help="Fail the job when the verdict reaches this level.")
    ] = FailOn.BLOCK,
    strict: Annotated[
        bool, typer.Option(help="Also fail the job when the review is incomplete.")
    ] = False,
    out_dir: Annotated[
        Path | None, typer.Option(help="Where to write the JSON and Markdown.")
    ] = None,
    repo: Annotated[Path, typer.Option(help="Checkout to review.")] = Path("."),
) -> None:
    """Review a pull request in GitHub Actions and post the result as one comment.

    Reads GITHUB_EVENT_PATH, GITHUB_REPOSITORY, and GITHUB_TOKEN from the environment.
    """
    import os

    opts = CiOptions(
        mode=mode, model=model, provider=provider, deep=deep, comment=comment, strict=strict,
        fail_on=fail_on.value, out_dir=out_dir,
        checks=[c.strip() for c in checks.split(",") if c.strip()] if checks else None,
        base=base, head=head,
    )  # fmt: skip
    try:
        outcome = run_ci(opts, dict(os.environ), cwd=repo.resolve())
    except CiError as e:
        err.print(f"[red]error:[/red] {e}")
        raise typer.Exit(EXIT_ERROR) from e
    render(outcome.result, out)
    for note in outcome.notes:
        err.print(f"[yellow]note:[/yellow] {note}")
    if outcome.comment_url:
        typer.echo(f"PR comment {outcome.comment_action}: {outcome.comment_url}")
    typer.echo(f"wrote {outcome.json_path} and {outcome.markdown_path}")
    raise typer.Exit(outcome.exit_code)


@hook_app.command("install")
def hook_install(
    mode: Annotated[
        ReviewMode, typer.Option(help="deterministic is free and fast; agent uses your LLM.")
    ] = ReviewMode.DETERMINISTIC,
    with_tests: Annotated[
        bool, typer.Option(help="Also run the project's tests on every push (slow).")
    ] = False,
    force: Annotated[bool, typer.Option(help="Replace an existing pre-push hook.")] = False,
    repo: Annotated[Path, typer.Option(help="Repository path.")] = Path("."),
) -> None:
    """Install a pre-push hook that blocks pushes whose review verdict is BLOCK."""
    try:
        path = hooks.install(
            repo_root(repo.resolve()), mode=mode.value, with_tests=with_tests, force=force
        )
    except (GitError, hooks.HookError) as e:
        err.print(f"[red]error:[/red] {e}")
        raise typer.Exit(EXIT_ERROR) from e
    typer.echo(f"installed {path}")
    typer.echo("skip once with `git push --no-verify` or `WATCHMAN_SKIP=1 git push`")


@hook_app.command("uninstall")
def hook_uninstall(
    repo: Annotated[Path, typer.Option(help="Repository path.")] = Path("."),
) -> None:
    """Remove the Watchman pre-push hook (restoring one it replaced)."""
    try:
        path = hooks.uninstall(repo_root(repo.resolve()))
    except (GitError, hooks.HookError) as e:
        err.print(f"[red]error:[/red] {e}")
        raise typer.Exit(EXIT_ERROR) from e
    typer.echo(f"removed {path}" if path else "no Watchman hook was installed")


@hook_app.command("status")
def hook_status(repo: Annotated[Path, typer.Option(help="Repository path.")] = Path(".")) -> None:
    """Show whether the pre-push hook is installed."""
    try:
        st = hooks.status(repo_root(repo.resolve()))
    except GitError as e:
        err.print(f"[red]error:[/red] {e}")
        raise typer.Exit(EXIT_ERROR) from e
    state = (
        "installed" if st.installed else "another hook is in the way" if st.foreign else "absent"
    )
    typer.echo(f"{st.path}: {state}")


SessionOpt = Annotated[
    str, typer.Option(envvar="WATCHMAN_SESSION", help="Session id (one worker session).")
]
RepoOpt = Annotated[Path, typer.Option(help="The repository the worker is editing.")]


@live_app.command("start")
def live_start(
    session: SessionOpt = "default",
    task: Annotated[str | None, typer.Option(help="What the worker was asked to do.")] = None,
    base: Annotated[str | None, typer.Option(help="Review everything since this ref.")] = None,
    repo: RepoOpt = Path("."),
) -> None:
    """Record where the worker started (the base) and its task."""
    from watchman_review.live import controller as live

    try:
        state = live.start(repo_root(repo.resolve()), session, task=task, base=base)
    except GitError as e:
        err.print(f"[red]error:[/red] {e}")
        raise typer.Exit(EXIT_ERROR) from e
    typer.echo(f"session {session}: base {(state.base or '-')[:10]}")


@live_app.command("check")
def live_check(
    kind: Annotated[str, typer.Option(help="test_run, commit, or done.")] = "done",
    session: SessionOpt = "default",
    task: Annotated[str | None, typer.Option(help="What the worker was asked to do.")] = None,
    policy: Annotated[
        LivePolicy | None, typer.Option(help="advisory or blocking (default: config).")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the feedback as JSON.")] = False,
    inbox: Annotated[bool, typer.Option(help="Also write .watchman/inbox.md.")] = False,
    repo: RepoOpt = Path("."),
) -> None:
    """Review a snapshot of the worker's tree and print terse feedback."""
    from watchman_review.live import controller as live

    try:
        root = repo_root(repo.resolve())
        fb = live.check(root, kind=kind, session=session, task=task, policy=policy)
    except (GitError, live.LiveError, ConfigError) as e:
        err.print(f"[red]error:[/red] {e}")
        raise typer.Exit(EXIT_ERROR) from e
    if inbox:
        live.write_inbox(root, session, fb)
    typer.echo(fb.model_dump_json(indent=2) if as_json else fb.render())


@live_app.command("status")
def live_status(session: SessionOpt = "default", repo: RepoOpt = Path(".")) -> None:
    """Show the supervised session: base, task, each checkpoint's round, and open findings."""
    from watchman_review.live import state as live_state

    try:
        state = live_state.load(repo_root(repo.resolve()), session)
    except GitError as e:
        err.print(f"[red]error:[/red] {e}")
        raise typer.Exit(EXIT_ERROR) from e
    if state is None:
        typer.echo(f"no live session {session!r} yet; run `watchman live start` or `check`")
        return
    typer.echo(
        f"session {state.session}  base {(state.base or '-')[:10]}  task: {state.task or '-'}"
    )
    for kind, ep in state.episodes.items():
        typer.echo(f"  {kind:9} {ep.status:9} round {ep.round}  open findings: {len(ep.seen)}")
        for seen in ep.seen.values():
            typer.echo(
                f"      {seen.severity} {seen.category} {seen.location} (r{seen.round}) {seen.summary}"
            )
    cost = sum(h.cost_usd for h in state.history)
    typer.echo(f"  {len(state.history)} review(s), ${cost:.4f} spent")


@live_app.command("reset")
def live_reset(
    kind: Annotated[str | None, typer.Option(help="Only this checkpoint kind.")] = None,
    session: SessionOpt = "default",
    repo: RepoOpt = Path("."),
) -> None:
    """Start the review/fix loop over (clears rounds, seen findings, and escalations)."""
    from watchman_review.live import controller as live

    live.reset(repo_root(repo.resolve()), session, kind)
    typer.echo("reset " + (kind or "all checkpoint kinds"))


@app.command()
def setup() -> None:
    """Interactive setup: provider, model, API key source, and budgets."""
    from watchman_review.setup_wizard import run_setup

    class TyperIO:
        def prompt(self, text: str, default: str | None = None, *, hide: bool = False) -> str:
            return str(typer.prompt(text, default=default, hide_input=hide, show_default=not hide))

        def confirm(self, text: str, default: bool = True) -> bool:
            return bool(typer.confirm(text, default=default))

        def echo(self, text: str) -> None:
            typer.echo(text)

    try:
        existing, _ = load_config(None)
    except ConfigError as e:
        err.print(f"[yellow]existing config is invalid ({e}); starting fresh[/yellow]")
        from watchman_review.config import Config

        existing = Config()
    cfg = run_setup(TyperIO(), existing)
    path = save_user_config(cfg)
    typer.echo(f"\nSaved {path} (mode 600). No API key values were written.")


@app.command()
def schema() -> None:
    """Print the JSON Schema of the review output."""
    typer.echo(json.dumps(ReviewResult.model_json_schema(), indent=2))


@app.command()
def budget() -> None:
    """Show this month's recorded LLM spend against the cap."""
    cfg, _ = load_config(None)
    ledger = Ledger()
    spent = ledger.month_total()
    cap = cfg.budget.monthly_cap_usd
    typer.echo(f"spent this month: ${spent:.4f} / cap ${cap:.2f}  (ledger: {ledger.path})")


@config_app.command("show")
def config_show(repo: Annotated[Path, typer.Option(help="Repository path.")] = Path(".")) -> None:
    """Print the effective configuration (never includes key values)."""
    try:
        root: Path | None = repo_root(repo.resolve())
    except GitError:
        root = None
    cfg, warnings = load_config(root)
    typer.echo(f"# user config: {user_config_path()}")
    for w in warnings:
        typer.echo(f"# warning: {w}")
    typer.echo(cfg.model_dump_json(indent=2))
