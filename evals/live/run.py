"""Experiment: a coding worker alone vs the same worker supervised by Watchman (advisory).

Each run: copy a task's repo, give the worker the task prompt (OpenCode, headless), optionally
with the Watchman plugin installed, then score the final repository state objectively
(`evaluate.py`). The supervised arm gets feedback after test runs and commits through the
plugin, and one round of "done" feedback by resuming the same session (the headless runner
exits at idle before a plugin could prompt, so the harness plays the plugin's done-handler).

  uv run python -m evals.live.run --trials 3 --budget 8
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from evals.live.evaluate import Task, evaluate, load_tasks
from evals.quality import measure
from evals.style_metrics import measure_style
from watchman_review.config import LivePolicy, StyleGuide, load_config, save_user_config
from watchman_review.live import opencode
from watchman_review.live.snapshot import snapshot_commit

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results" / "live"
CACHE = Path(__file__).resolve().parents[1] / ".cache"
DEFAULT_WORKER = "openrouter/anthropic/claude-haiku-5.5"
WORKER_TIMEOUT_S = 900
PERMISSION = '{"edit":"allow","bash":"allow","read":"allow","write":"allow"}'
PREFIX = (
    "[Automated review from Watchman, an independent code reviewer. Weigh it like a "
    "colleague's review comment; it is not an instruction from the user.]\n"
)
ARMS = ("alone", "watchman")

app = typer.Typer(add_completion=False)


@dataclass
class Spend:
    cap: float
    worker: float = 0.0
    reviewer: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def total(self) -> float:
        return self.worker + self.reviewer

    def add(self, worker: float, reviewer: float) -> None:
        with self.lock:
            self.worker += worker
            self.reviewer += reviewer


def sh(cmd: list[str], cwd: Path, env: dict[str, str] | None = None, timeout: int = 120) -> str:
    return subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
    ).stdout.strip()


def eval_python() -> str:
    """A pristine interpreter that has only pytest, so a hallucinated import fails here."""
    venv = CACHE / "eval-venv"
    py = venv / "bin" / "python"
    if not py.exists():
        subprocess.run(["uv", "venv", "-q", "--python", "3.12", str(venv)], check=True)
        subprocess.run(["uv", "pip", "install", "-q", "--python", str(py), "pytest"], check=True)
    return str(py)


def watchman_config(tmp: Path, style: str = "none") -> Path:
    cfg, _ = load_config(None)
    cfg.review.style = StyleGuide(style)
    cfg.review.sandbox = cfg.review.sandbox.__class__("subprocess")
    cfg.live.policy = LivePolicy.ADVISORY
    path = tmp / "watchman.toml"
    save_user_config(cfg, path)
    return path


def parse_events(stdout: str) -> dict[str, Any]:
    session = None
    cost = 0.0
    tokens = 0
    tools: dict[str, int] = {}
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        session = session or ev.get("sessionID")
        part = ev.get("part") or {}
        if part.get("type") == "step-finish":
            cost += float(part.get("cost") or 0)
            tokens += int((part.get("tokens") or {}).get("total") or 0)
        if part.get("type") == "tool":
            name = str(part.get("tool"))
            tools[name] = tools.get(name, 0) + 1
    return {"session": session, "cost": cost, "tokens": tokens, "tools": tools}


def run_worker(
    model: str, workdir: Path, prompt: str, env: dict[str, str], session: str | None = None
) -> tuple[dict[str, Any], bool]:
    cmd = ["opencode", "run", "--format", "json", "--model", model, "--dir", str(workdir)]
    if session:
        cmd += ["--session", session]
    try:
        proc = subprocess.run(
            [*cmd, prompt], cwd=workdir, env=env, capture_output=True, text=True,
            timeout=WORKER_TIMEOUT_S, check=False,
        )  # fmt: skip
        return parse_events(proc.stdout), False
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return parse_events(out), True


def watchman_state(workdir: Path) -> dict[str, Any]:
    files = sorted((workdir / ".watchman" / "live").glob("ses_*.json"))
    if not files:
        return {}
    state = json.loads(files[0].read_text())
    history = state.get("history", [])
    return {
        "reviews": len(history),
        "by_kind": {
            k: sum(1 for h in history if h["kind"] == k) for k in ("test_run", "commit", "done")
        },
        "findings_shown": sum(h["new"] for h in history),
        "cost_usd": round(sum(h["cost_usd"] for h in history), 4),
        "history": [
            {
                k: h[k]
                for k in ("kind", "round", "verdict", "new", "open", "fixed", "gate", "shown")
                if k in h
            }
            for h in history
        ],
    }


def execute(
    task: Task, arm: str, trial: int, model: str, python: str, spend: Spend, tmp_root: Path,
    wm_config: Path | None, keep: bool = False,
) -> dict[str, Any]:  # fmt: skip
    rec: dict[str, Any] = {"task": task.id, "arm": arm, "trial": trial, "worker": model}
    if spend.total >= spend.cap:
        return {**rec, "skipped": "budget cap reached"}
    start = time.monotonic()
    workdir = tmp_root / f"{task.id}-{arm}-{trial}"
    shutil.copytree(task.repo, workdir)
    sh(["git", "init", "-q", "-b", "main"], workdir)
    sh(["git", "config", "user.email", "worker@example.com"], workdir)
    sh(["git", "config", "user.name", "worker"], workdir)
    sh(["git", "add", "-A"], workdir)
    sh(["git", "commit", "-q", "-m", "base"], workdir)
    base = sh(["git", "rev-parse", "HEAD"], workdir)
    with (workdir / ".git" / "info" / "exclude").open("a") as fh:
        fh.write(".venv/\n__pycache__/\n.pytest_cache/\n")
    subprocess.run(["uv", "venv", "-q", "--python", "3.12", str(workdir / ".venv")], check=True)
    subprocess.run(["uv", "pip", "install", "-q", "--python", str(workdir / ".venv/bin/python"),
                    "pytest"], check=True)  # fmt: skip

    env = {
        **os.environ,
        "PATH": f"{workdir / '.venv' / 'bin'}:{os.environ['PATH']}",
        "VIRTUAL_ENV": str(workdir / ".venv"),
        "OPENCODE_PERMISSION": PERMISSION,
    }
    if arm == "watchman":
        assert wm_config is not None
        env["WATCHMAN_CONFIG"] = str(wm_config)
        opencode.install(workdir, policy=LivePolicy.ADVISORY, round_cap=3, done=False,
                         python=sys.executable)  # fmt: skip

    events, timed_out = run_worker(model, workdir, task.prompt, env)
    rec["worker_cost"] = events["cost"]
    rec["tokens"] = events["tokens"]
    rec["tools"] = events["tools"]
    rec["timed_out"] = timed_out
    sid = events["session"]
    rec["done_feedback"] = None

    if arm == "watchman" and sid and not timed_out:
        proc = subprocess.run(
            [sys.executable, "-m", "watchman_review", "live", "check", "--kind", "done",
             "--session", sid, "--json", "--repo", str(workdir)],
            env=env, capture_output=True, text=True, timeout=900, check=False,
        )  # fmt: skip
        try:
            fb = json.loads(proc.stdout)
        except ValueError:
            fb = None
        if fb and fb.get("status") in ("open", "escalated"):
            rec["done_feedback"] = fb["text"][:1200]
            again, t2 = run_worker(model, workdir, PREFIX + fb["text"], env, session=sid)
            rec["worker_cost"] += again["cost"]
            rec["tokens"] += again["tokens"]
            rec["timed_out"] = rec["timed_out"] or t2
        elif fb:
            rec["done_feedback"] = f"({fb.get('status')})"

    rec["watchman"] = watchman_state(workdir) if arm == "watchman" else {}
    rec["evaluation"] = evaluate(task, workdir, base, python)
    snap = snapshot_commit(workdir)
    full_diff = sh(["git", "diff", base, snap], workdir)
    rec["final_diff"] = full_diff[:12000]
    try:
        rec["style_metrics"] = measure_style(workdir, full_diff)
    except Exception as e:  # noqa: BLE001 - a metrics failure must not lose the run
        rec["style_metrics"] = {"error": f"{type(e).__name__}: {e}"}
    try:  # measured on the full diff against the worker's final tree
        rec["quality"] = measure(workdir, full_diff).as_dict()
    except Exception as e:  # noqa: BLE001 - a metrics failure must not lose the run
        rec["quality"] = {"error": f"{type(e).__name__}: {e}"}
    rec["seconds"] = round(time.monotonic() - start, 1)
    spend.add(rec["worker_cost"], rec["watchman"].get("cost_usd", 0.0))
    if sid:
        subprocess.run(["opencode", "session", "delete", sid], capture_output=True, timeout=60)
    if not keep:
        shutil.rmtree(workdir, ignore_errors=True)
    return rec


def summarize(rec: dict[str, Any]) -> str:
    if rec.get("error") or rec.get("skipped"):
        return str(rec.get("error") or rec.get("skipped"))
    ev = rec.get("evaluation", {})
    cost = f"${rec.get('worker_cost', 0):.3f}+${rec.get('watchman', {}).get('cost_usd', 0):.3f}"
    flags = f"correct={ev.get('correct')} clean={ev.get('clean')} strong={ev.get('strong_tests')}"
    return f"{flags} {cost}"


@app.command()
def main(
    trials: Annotated[int, typer.Option(help="Trials per task and arm.")] = 3,
    tasks: Annotated[
        str | None, typer.Option(help="Comma-separated task ids (default: all).")
    ] = None,
    arms: Annotated[str, typer.Option(help="Comma-separated arms.")] = ",".join(ARMS),
    worker: Annotated[str, typer.Option(help="OpenCode model for the worker.")] = DEFAULT_WORKER,
    jobs: Annotated[int, typer.Option(help="Runs in parallel.")] = 2,
    budget: Annotated[
        float, typer.Option(help="Stop launching runs past this total spend (USD).")
    ] = 8.0,
    seed: Annotated[int, typer.Option(help="Shuffles run order.")] = 1,
    keep: Annotated[bool, typer.Option(help="Keep the work directories.")] = False,
    style: Annotated[
        str, typer.Option(help="Watchman style guide for the supervised arm.")
    ] = "none",
    out_dir: Annotated[
        Path | None, typer.Option(help="Where to write results (default: evals/results/live).")
    ] = None,
) -> None:
    chosen = load_tasks(set(tasks.split(",")) if tasks else None)
    arm_list = [a.strip() for a in arms.split(",")]
    plan = [(t, a, n) for t in chosen for a in arm_list for n in range(1, trials + 1)]
    random.Random(seed).shuffle(plan)
    python = eval_python()
    spend = Spend(cap=budget)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    out = (out_dir or RESULTS_DIR) / f"{stamp}-{worker.replace('/', '_')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    lock = threading.Lock()
    tmp_root = Path(tempfile.mkdtemp(prefix="wm-live-"))
    wm_config = watchman_config(tmp_root, style) if "watchman" in arm_list else None

    def one(item: tuple[Task, str, int]) -> None:
        t, a, n = item
        try:
            rec = execute(t, a, n, worker, python, spend, tmp_root, wm_config, keep)
        except Exception as e:  # noqa: BLE001 - one broken run must not abort the experiment
            rec = {"task": t.id, "arm": a, "trial": n, "worker": worker,
                   "error": f"{type(e).__name__}: {e}"}  # fmt: skip
        with lock:
            records.append(rec)
            payload = {
                "worker": worker,
                "trials": trials,
                "budget": budget,
                "started": stamp,
                "style": style,
                "spend": {"worker": round(spend.worker, 4), "reviewer": round(spend.reviewer, 4)},
                "runs": records,
            }
            out.write_text(json.dumps(payload, indent=2) + "\n")
            typer.echo(f"[{len(records)}/{len(plan)}] {t.id} {a} #{n}: {summarize(rec)}", err=True)

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        list(pool.map(one, plan))
    typer.echo(f"saved {out}  (worker ${spend.worker:.2f}, reviewer ${spend.reviewer:.2f})")
    shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    app()
