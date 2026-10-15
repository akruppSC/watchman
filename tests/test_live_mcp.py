from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp", reason="needs the optional `live` extra")

from tests.conftest import commit_files
from watchman_review.live import controller
from watchman_review.live.mcp_server import feedback_text


@pytest.fixture
def repo(git_repo: Path) -> Path:
    commit_files(git_repo, {"pyproject.toml": '[project]\nname="x"\ndependencies=[]\n',
                            "a.py": "x = 1\n"}, "base")  # fmt: skip
    (git_repo / "a.py").write_text("try:\n    x = 1\nexcept:\n    pass\n")
    return git_repo


def test_feedback_text_runs_the_real_checkpoint_controller(repo: Path) -> None:
    text = feedback_text(repo, "s", "test_run", None)
    assert text.startswith("WATCHMAN test_run round 1/3")
    assert "swallowed_error a.py" in text and "E722" in text
    again = feedback_text(repo, "s", "test_run", None)
    assert "no changes since the last review" in again


def test_feedback_text_never_raises_for_the_worker(tmp_path: Path) -> None:
    assert "unknown kind 'lunch'" in feedback_text(tmp_path, "s", "lunch", None)
    assert "could not review" in feedback_text(tmp_path / "missing", "s", "done", None)
    assert controller.KINDS == ("test_run", "commit", "done")


class McpProcess:
    """A real MCP server over stdio, driven with raw newline-delimited JSON-RPC."""

    def __init__(self, repo: Path) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "watchman_review", "live", "mcp", "--repo", str(repo)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=os.environ.copy(),
        )  # fmt: skip
        self.next_id = 0

    def send(self, msg: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        assert self.proc.stdout is not None
        deadline = time.time() + 120
        while time.time() < deadline:
            ready, _, _ = select.select([self.proc.stdout], [], [], 1.0)
            if ready:
                msg = json.loads(self.proc.stdout.readline())
                if msg.get("id") == self.next_id:
                    return msg
        raise TimeoutError(f"no response to {method}")

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=10)


def test_the_mcp_server_speaks_the_protocol_and_serves_the_tool(repo: Path) -> None:
    server = McpProcess(repo)
    try:
        init = server.call("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        })  # fmt: skip
        assert init["result"]["serverInfo"]["name"] == "watchman"
        server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = server.call("tools/list", {})["result"]["tools"]
        assert [t["name"] for t in tools] == ["get_review_feedback"]
        assert "kind" in tools[0]["inputSchema"]["properties"]
        out = server.call("tools/call", {"name": "get_review_feedback",
                                         "arguments": {"kind": "test_run"}})  # fmt: skip
        text = out["result"]["content"][0]["text"]
        assert text.startswith("WATCHMAN test_run") and "swallowed_error" in text
    finally:
        server.close()
