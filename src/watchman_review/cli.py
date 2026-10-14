"""Command-line interface."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from watchman_review import __version__
from watchman_review.budget import Ledger
from watchman_review.config import (
    ConfigError,
    load_config,
    save_user_config,
    user_config_path,
)
from watchman_review.gitutil import WORKTREE, GitError, repo_root
from watchman_review.llm.cache import CacheMode
from watchman_review.report import render
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
        ReviewMode, typer.Option(help="Which reviewer to run.")
    ] = ReviewMode.DETERMINISTIC,
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
    fail_on: Annotated[
        FailOn, typer.Option(help="Exit 1 when the verdict reaches this level.")
    ] = FailOn.BLOCK,
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

    if task_file is not None:
        task = task_file.read_text()
    opts = ReviewOptions(
        base=base,
        head=WORKTREE if worktree else head,
        mode=mode,
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
    if as_json:
        typer.echo(payload)
    else:
        render(result, out)
    raise typer.Exit(gate_exit_code(result.verdict, fail_on))


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
