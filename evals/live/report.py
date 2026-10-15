"""Turn saved experiment runs into the results table (evals/results/LIVE.md).

  uv run python -m evals.live.report

"Good" means the final state is correct (hidden spec and visible tests pass), clean (no
pitfall probe fired), and strongly tested (the worker's tests reject every wrong
implementation). Arms are compared run-for-run: trial N of a task in one arm against trial N
in the other. With this few runs, read the counts, not the percentages.
"""

from __future__ import annotations

import json
from collections import defaultdict
from math import comb
from pathlib import Path
from typing import Any

import typer

from evals.harness import wilson

RESULTS = Path(__file__).resolve().parents[1] / "results"
LIVE_DIR = RESULTS / "live"

app = typer.Typer(add_completion=False)


TASKS_DIR = Path(__file__).resolve().parent / "tasks"


def post_hoc_tasks() -> set[str]:
    """Tasks added after the first results were seen (flagged in their task.json)."""
    out: set[str] = set()
    for f in TASKS_DIR.glob("*/task.json"):
        if json.loads(f.read_text()).get("post_hoc"):
            out.add(f.parent.name)
    return out


def merge(datasets: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine result files of one worker. A later run of the same (task, arm, trial)
    replaces an earlier one, unless the later one was skipped (budget cap)."""
    merged: dict[tuple[str, str, int], dict[str, Any]] = {}
    for d in datasets:
        for r in d["runs"]:
            key = (r["task"], r["arm"], r["trial"])
            if r.get("skipped") and key in merged:
                continue
            merged[key] = r
    runs = list(merged.values())
    spend = {
        "worker": sum(r.get("worker_cost", 0.0) for r in runs),
        "reviewer": sum(r.get("watchman", {}).get("cost_usd", 0.0) for r in runs),
    }
    return {"worker": datasets[0]["worker"], "spend": spend, "runs": runs}


def is_done(rec: dict[str, Any]) -> bool:
    return "evaluation" in rec and not rec.get("error") and not rec.get("skipped")


def verdicts(rec: dict[str, Any]) -> dict[str, bool]:
    ev = rec["evaluation"]
    strong = ev["strong_tests"] is not False
    return {
        "correct": bool(ev["correct"]),
        "clean": bool(ev["clean"]),
        "strong": bool(strong),
        "good": bool(ev["correct"] and ev["clean"] and strong),
    }


def sign_test(wins: int, losses: int) -> float:
    """Two-sided exact binomial (sign) test p-value on the discordant pairs."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    p = 2 * sum(comb(n, i) for i in range(k + 1)) / float(2**n)
    return float(min(1.0, p))


def pairs(runs: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    by_key: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in runs:
        if is_done(r):
            by_key[(r["task"], r["trial"])][r["arm"]] = r
    return [
        (v["alone"], v["watchman"]) for v in by_key.values() if {"alone", "watchman"} <= v.keys()
    ]


def rate(k: int, n: int) -> str:
    if not n:
        return "-"
    lo, hi = wilson(k, n)
    return f"{k}/{n} ({k / n * 100:.0f}%) [{lo * 100:.0f}-{hi * 100:.0f}]"


def render(data: dict[str, Any], post_hoc: frozenset[str] | set[str] = frozenset()) -> str:
    runs: list[dict[str, Any]] = data["runs"]
    done = [r for r in runs if is_done(r)]
    arms = sorted({r["arm"] for r in done}, key=lambda a: (a != "alone", a))
    lines = [
        f"## Worker `{data['worker']}`: alone vs supervised by Watchman (advisory)",
        "",
        f"{len(done)} completed runs of {len(runs)} started; spend: worker "
        f"${data['spend']['worker']:.2f}, reviewer ${data['spend']['reviewer']:.2f}. "
        "Counts are runs; [..] is a 95% Wilson interval.",
        "",
        "| arm | runs | correct | clean | strong tests | all good | worker $/run "
        "| reviewer $/run | seconds |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for arm in arms:
        rs = [r for r in done if r["arm"] == arm]
        v = [verdicts(r) for r in rs]
        n = len(rs)
        cells = [rate(sum(x[k] for x in v), n) for k in ("correct", "clean", "strong", "good")]
        wc = sum(r["worker_cost"] for r in rs) / n
        rc = sum(r["watchman"].get("cost_usd", 0.0) for r in rs) / n
        secs = sum(r["seconds"] for r in rs) / n
        lines.append(
            f"| {arm} | {n} | " + " | ".join(cells) + f" | ${wc:.3f} | ${rc:.3f} | {secs:.0f} |"
        )

    ps = pairs(runs)
    if ps:
        wins = losses = both = neither = 0
        for a, w in ps:
            ga, gw = verdicts(a)["good"], verdicts(w)["good"]
            wins += gw and not ga
            losses += ga and not gw
            both += ga and gw
            neither += not ga and not gw
        lines += [
            "",
            f"**Paired runs (same task and trial): {len(ps)}.** Both good: {both}. Neither: "
            f"{neither}. Only the supervised run good: {wins}. Only the unsupervised run good: "
            f"{losses}. Sign test p = {sign_test(wins, losses):.2f}.",
        ]
        for key, label in (("correct", "correct"), ("clean", "clean"), ("strong", "strong tests")):
            w_ = sum(verdicts(w)[key] and not verdicts(a)[key] for a, w in ps)
            l_ = sum(verdicts(a)[key] and not verdicts(w)[key] for a, w in ps)
            lines.append(f"- {label}: supervised better in {w_} pair(s), worse in {l_}.")

    lines += [
        "",
        "### By task (all-good runs out of runs)",
        "",
        *(
            [
                "† = added after the first results showed a ceiling effect (post hoc), so these "
                "were not planned in advance.",
                "",
            ]
            if post_hoc & {r["task"] for r in done}
            else []
        ),
        "| task | alone | supervised | pitfalls probed |",
        "|---|---|---|---|",
    ]
    tasks = sorted({r["task"] for r in done})
    for t in tasks:
        cell = []
        for arm in ("alone", "watchman"):
            rs = [r for r in done if r["task"] == t and r["arm"] == arm]
            cell.append(f"{sum(verdicts(r)['good'] for r in rs)}/{len(rs)}")
        hits: dict[str, int] = defaultdict(int)
        for r in done:
            if r["task"] == t:
                for probe in r["evaluation"]["probe_hits"]:
                    hits[probe.split(":")[0]] += 1
        probed = ", ".join(f"{k} x{v}" for k, v in hits.items()) or "-"
        lines.append(f"| {t}{' †' if t in post_hoc else ''} | {cell[0]} | {cell[1]} | {probed} |")

    sup = [r for r in done if r["arm"] == "watchman"]
    if sup:
        mean_reviews = sum(r["watchman"].get("reviews", 0) for r in sup) / len(sup)
        shown = [r for r in sup if r["watchman"].get("findings_shown", 0) > 0]
        resumed = [r for r in sup if (r.get("done_feedback") or "").startswith("WATCHMAN")]
        lines += [
            "",
            "### What Watchman did in the supervised runs",
            "",
            f"- Reviews per run: {mean_reviews:.1f} (test-run, commit and done checkpoints).",
            "- Runs where Watchman showed the worker at least one finding: "
            f"{len(shown)} of {len(sup)}.",
            f"- Runs resumed with done-feedback: {len(resumed)}.",
        ]
        if shown:
            good = sum(verdicts(r)["good"] for r in shown)
            lines.append(f"- Of the runs it spoke up in, {good} of {len(shown)} ended all good.")
    timeouts = sum(1 for r in done if r.get("timed_out"))
    if timeouts:
        lines += ["", f"{timeouts} run(s) hit the worker timeout; they are scored as left."]
    return "\n".join(lines) + "\n"


@app.command()
def main() -> None:
    files = sorted(LIVE_DIR.glob("*.json"))
    if not files:
        typer.echo("no experiment results yet", err=True)
        raise typer.Exit(1)
    by_worker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in files:  # oldest first, so later runs override earlier ones
        data = json.loads(f.read_text())
        by_worker[data["worker"]].append(data)
    ph = post_hoc_tasks()
    body = "\n".join(render(merge(ds), ph) for ds in by_worker.values())
    out = RESULTS / "LIVE.md"
    out.write_text(
        "# Live-mode experiment\n\nGenerated by `python -m evals.live.report`.\n\n" + body
    )
    typer.echo(f"wrote {out}")


if __name__ == "__main__":
    app()
