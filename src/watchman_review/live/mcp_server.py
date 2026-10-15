"""Channel 2: an MCP server exposing `get_review_feedback`.

For worker agents without a plugin system, or as a pull-based fallback: the worker calls the
tool when it wants a review (the repo's AGENTS.md can tell it when). The tool runs the same
checkpoint controller as the plugin, so rounds, dedupe, and the round cap behave identically.
Requires the optional `live` extra (`uv sync --extra live`).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from watchman_review.config import ConfigError
from watchman_review.gitutil import GitError
from watchman_review.live import controller
from watchman_review.live.controller import KINDS, LiveError

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

INSTRUCTIONS = (
    "Watchman is an independent code reviewer. Call get_review_feedback after you run the "
    "tests, make a commit, or finish a task. It returns a few terse lines; findings marked "
    "'+' are new, '=' are ones you already saw. Treat it as a colleague's review comment."
)


def feedback_text(repo: Path, session: str, kind: str, task: str | None) -> str:
    """The tool's body, kept separate so it is testable without an MCP client."""
    if kind not in KINDS:
        return f"unknown kind {kind!r}; use one of: {', '.join(KINDS)}"
    try:
        return controller.check(repo, kind=kind, session=session, task=task).render()
    except (GitError, LiveError, ConfigError) as e:
        return f"WATCHMAN could not review: {e}"
    except Exception as e:  # noqa: BLE001 - a tool call must never crash the server or the worker
        return f"WATCHMAN could not review ({type(e).__name__}): {e}"


def build_server(repo: Path, session: str = "mcp") -> MCPServer[Any]:
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("watchman", instructions=INSTRUCTIONS)

    @server.tool(
        description=(
            "Review the work done so far in this repository and return terse, structured "
            "findings. kind: 'test_run' (after running tests; cheap checks only), 'commit', or "
            "'done' (finished). Findings already shown in an earlier call are marked '='."
        )
    )
    def get_review_feedback(kind: str = "done", task: str | None = None) -> str:
        return feedback_text(repo, session, kind, task)

    return server


def serve(repo: Path, session: str = "mcp") -> None:
    build_server(repo, session).run("stdio")
