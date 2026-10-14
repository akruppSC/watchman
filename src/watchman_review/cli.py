"""Command-line interface."""

from __future__ import annotations

import typer

from watchman_review import __version__

app = typer.Typer(
    name="watchman",
    help="AI reviewer for code written by AI coding agents.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """AI reviewer for code written by AI coding agents."""


@app.command()
def version() -> None:
    """Print the Watchman version."""
    typer.echo(__version__)
