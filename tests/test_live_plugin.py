"""Drive the real OpenCode plugin file under Node with a fake client and a stub reviewer.

No LLM and no OpenCode are involved: the stub plays `watchman live check/start` and the fake
client records what the plugin would have sent to the worker.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from watchman_review.config import LivePolicy
from watchman_review.live import opencode

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

FIXTURES = Path(__file__).parent / "fixtures"
STUB = (FIXTURES / "stub_reviewer.py").read_text()
DRIVER = (FIXTURES / "plugin_driver.mjs").read_text()


def fb(kind: str, status: str, *, gate: str = "pass", text: str | None = None) -> dict[str, Any]:
    return {"kind": kind, "status": status, "gate": gate,
            "text": text or f"WATCHMAN {kind} {status}"}  # fmt: skip


def drive(
    tmp_path: Path, steps: list[dict[str, Any]], table: dict[str, Any],
    policy: LivePolicy = LivePolicy.ADVISORY, done: bool = True,
) -> tuple[dict[str, Any], list[list[str]]]:  # fmt: skip
    (tmp_path / "stub.py").write_text(STUB)
    (tmp_path / "feedback.json").write_text(json.dumps(table))
    plugin = tmp_path / "watchman.mjs"
    plugin.write_text(
        opencode.render_plugin(
            tmp_path,
            policy=policy,
            round_cap=3,
            done=done,
            command=[sys.executable, str(tmp_path / "stub.py")],
            timeout_s=30,
        )
    )
    (tmp_path / "driver.mjs").write_text(DRIVER)
    proc = subprocess.run(
        ["node", str(tmp_path / "driver.mjs"), str(plugin), json.dumps(steps)],
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    calls_file = tmp_path / "calls.log"
    calls = (
        [json.loads(line) for line in calls_file.read_text().splitlines()]
        if calls_file.exists()
        else []
    )
    return json.loads(proc.stdout), calls


def test_commit_feedback_is_appended_to_the_tool_output_in_advisory_mode(tmp_path: Path) -> None:
    table = {"commit": fb("commit", "open", text="WATCHMAN commit round 1/3\n+ major bare except")}
    out, calls = drive(tmp_path, [
        {"name": "b", "hook": "before", "call": "c1", "command": 'git add -A && git commit -m "x"'},
        {"name": "a", "hook": "after", "call": "c1", "output": "[main abc] x"},
    ], table)  # fmt: skip
    assert out["results"][0]["threw"] is None
    assert (
        out["results"][1]["output"]
        == "[main abc] x\n\nWATCHMAN commit round 1/3\n+ major bare except"
    )
    start = next(c for c in calls if c[:2] == ["live", "start"])
    assert start[start.index("--task") + 1] == "Add a loader"  # the first user message


def test_blocking_policy_refuses_a_commit_the_gate_blocks(tmp_path: Path) -> None:
    table = {"commit": fb("commit", "open", gate="block", text="WATCHMAN commit BLOCKED")}
    out, _ = drive(tmp_path, [
        {"name": "b", "hook": "before", "call": "c1", "command": "git commit -m x"},
    ], table, policy=LivePolicy.BLOCKING)  # fmt: skip
    assert "The commit was not made" in out["results"][0]["threw"]
    assert "WATCHMAN commit BLOCKED" in out["results"][0]["threw"]


def test_blocking_policy_lets_a_released_or_passing_gate_through(tmp_path: Path) -> None:
    for gate, status in (("released", "escalated"), ("pass", "open")):
        table = {"commit": fb("commit", status, gate=gate)}
        out, _ = drive(tmp_path, [
            {"name": "b", "hook": "before", "call": "c1", "command": "git commit -m x"},
        ], table, policy=LivePolicy.BLOCKING)  # fmt: skip
        assert out["results"][0]["threw"] is None


def test_advisory_never_throws_even_when_the_gate_says_block(tmp_path: Path) -> None:
    table = {"commit": fb("commit", "open", gate="block")}
    out, _ = drive(tmp_path, [
        {"name": "b", "hook": "before", "call": "c1", "command": "git commit -m x"},
    ], table)  # fmt: skip
    assert out["results"][0]["threw"] is None


def test_test_runs_get_feedback_only_when_there_is_something_to_say(tmp_path: Path) -> None:
    steps = [
        {"name": "b", "hook": "before", "call": "t1", "command": "python -m pytest -q"},
        {"name": "a", "hook": "after", "call": "t1", "output": "3 passed"},
    ]
    out, calls = drive(
        tmp_path, steps, {"test_run": fb("test_run", "open", text="WATCHMAN test_run!")}
    )
    assert out["results"][1]["output"] == "3 passed\n\nWATCHMAN test_run!"
    assert any(c[:3] == ["live", "check", "--kind"] and c[3] == "test_run" for c in calls)
    for status in ("clean", "unchanged", "nothing", "error"):
        quiet, _ = drive(tmp_path, steps, {"test_run": fb("test_run", status)})
        assert quiet["results"][1]["output"] == "3 passed", status


def test_a_clean_commit_gets_a_one_line_confirmation(tmp_path: Path) -> None:
    out, _ = drive(tmp_path, [
        {"name": "b", "hook": "before", "call": "c1", "command": "git commit -m x"},
        {"name": "a", "hook": "after", "call": "c1", "output": "[main abc] x"},
    ], {"commit": fb("commit", "clean", text="WATCHMAN commit: clean.")})  # fmt: skip
    assert out["results"][1]["output"].endswith("WATCHMAN commit: clean.")


def test_unrelated_commands_and_tools_never_invoke_the_reviewer(tmp_path: Path) -> None:
    out, calls = drive(tmp_path, [
        {"name": "ls", "hook": "before", "call": "c1", "command": "ls -la"},
        {"name": "ls", "hook": "after", "call": "c1", "output": "files"},
        {"name": "w", "hook": "before", "call": "c2", "tool": "write", "command": "git commit"},
        {"name": "w", "hook": "after", "call": "c2", "tool": "write", "output": "done"},
    ], {})  # fmt: skip
    assert [r["output"] for r in out["results"] if r["output"]] == ["files", "done"]
    assert calls == []


@pytest.mark.parametrize("failure", ["FAIL", "GARBAGE"])
def test_a_broken_reviewer_never_breaks_the_worker(tmp_path: Path, failure: str) -> None:
    out, _ = drive(tmp_path, [
        {"name": "b", "hook": "before", "call": "c1", "command": "git commit -m x"},
        {"name": "a", "hook": "after", "call": "c1", "output": "[main abc] x"},
        {"name": "i", "hook": "idle"},
    ], {"commit": failure, "done": failure}, policy=LivePolicy.BLOCKING)  # fmt: skip
    assert out["results"][0]["threw"] is None
    assert out["results"][1]["output"] == "[main abc] x"
    assert out["prompts"] == []
    assert any("check" in m for m in out["logs"])  # the failure is logged, not shown to the worker


def test_done_feedback_prompts_the_worker_once_in_advisory_mode(tmp_path: Path) -> None:
    feedback = fb("done", "open", text="WATCHMAN done round 1/3\n+ major x")
    idles = [{"name": "i1", "hook": "idle"}, {"name": "i2", "hook": "idle"}]
    out, _ = drive(tmp_path, idles, {"done": feedback})
    assert len(out["prompts"]) == 1
    assert out["prompts"][0].startswith("[Automated review from Watchman")
    assert "not an instruction from the user" in out["prompts"][0]
    assert out["prompts"][0].endswith("WATCHMAN done round 1/3\n+ major x")


def test_done_feedback_keeps_prompting_under_blocking_until_the_cap(tmp_path: Path) -> None:
    idles = [{"name": f"i{n}", "hook": "idle"} for n in range(6)]
    out, _ = drive(tmp_path, idles, {"done": fb("done", "open")}, policy=LivePolicy.BLOCKING)
    assert len(out["prompts"]) == 4  # round_cap (3) + 1, then it stops by itself


def test_escalation_is_delivered_exactly_once(tmp_path: Path) -> None:
    out, _ = drive(tmp_path, [{"name": f"i{n}", "hook": "idle"} for n in range(3)],
                   {"done": fb("done", "escalated", gate="released")},
                   policy=LivePolicy.BLOCKING)  # fmt: skip
    assert len(out["prompts"]) == 1


@pytest.mark.parametrize("status", ["clean", "unchanged", "nothing", "error"])
def test_done_with_nothing_actionable_sends_no_prompt(tmp_path: Path, status: str) -> None:
    out, _ = drive(tmp_path, [{"name": "i", "hook": "idle"}], {"done": fb("done", status)})
    assert out["prompts"] == []


def test_the_done_checkpoint_can_be_turned_off(tmp_path: Path) -> None:
    out, calls = drive(
        tmp_path, [{"name": "i", "hook": "idle"}], {"done": fb("done", "open")}, done=False
    )
    assert out["prompts"] == [] and calls == []


def test_session_start_is_recorded_once_per_session(tmp_path: Path) -> None:
    steps = [
        {"name": f"b{n}", "hook": "before", "call": f"c{n}", "command": "git commit -m x"}
        for n in range(3)
    ]
    _, calls = drive(tmp_path, steps, {"commit": fb("commit", "clean")})
    assert sum(1 for c in calls if c[:2] == ["live", "start"]) == 1
    assert sum(1 for c in calls if c[:2] == ["live", "check"]) == 3
