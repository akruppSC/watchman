"""Experiment 3 results: Google-style supervision vs. none, graded by Google's own pylintrc.

  uv run python -m evals.live.style_report

Reads evals/results/live_style/*.json (written by `evals.live.run --style google --out-dir ...`)
and writes evals/results/STYLE.md. Paired by (task, trial); exact sign test on discordant pairs.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import typer

from evals.live.report import RESULTS, is_done, merge, sign_test

STYLE_DIR = RESULTS / "live_style"
app = typer.Typer(add_completion=False)


def share(num: int, den: int) -> float | None:
    return num / den if den else None


def metrics(rec: dict[str, Any]) -> dict[str, float | None]:
    s, q = rec["style_metrics"], rec["quality"]
    return {
        "pylint": float(s["pylint"]),
        "pylint_per_100_lines": share(s["pylint"] * 100, s["prod_lines_added"]),
        "documented": share(s["documented"], s["public_defs"]),
        "annotated": share(s["annotated"], s["public_funcs"]),
        "long_functions": float(s["long_functions"]),
        "breaches": float(q["breaches"]),
        "tests_added": float(q["tests_added"]),
        "prod_lines": float(s["prod_lines_added"]),
    }


ROWS = (
    ("pylint", "pylint messages, Google rc (primary)", False),
    ("pylint_per_100_lines", "pylint messages per 100 added lines", False),
    ("documented", "public API with a docstring (share)", True),
    ("annotated", "public functions fully annotated (share)", True),
    ("long_functions", "functions over 40 lines", False),
    ("breaches", "functions over a quality limit (40/60-line, 10 complexity, ...)", False),
    ("prod_lines", "production lines added", False),
    ("tests_added", "tests added", False),
)


def avg(xs: list[float | None]) -> str:
    vals = [x for x in xs if x is not None]
    return f"{sum(vals) / len(vals):.2f}" if vals else "-"


def render(data: dict[str, Any]) -> str:
    done = [r for r in data["runs"] if is_done(r) and "error" not in r.get("style_metrics", {})]
    by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in done:
        by_arm[r["arm"]].append(r)
    pairs: dict[tuple[str, int], dict[str, dict[str, float | None]]] = defaultdict(dict)
    for r in done:
        pairs[(r["task"], r["trial"])][r["arm"]] = metrics(r)
    paired = [v for v in pairs.values() if {"alone", "watchman"} <= v.keys()]
    lines = [
        f"## Worker `{data['worker']}`: alone vs. Watchman with `style = {data.get('style', '?')}`",
        "",
        f"{len(done)} completed runs ({len(paired)} paired); spend: worker "
        f"${data['spend']['worker']:.2f}, reviewer ${data['spend']['reviewer']:.2f}. Cells are "
        "means per run; the last column counts pairs where supervised was lower / higher "
        "(exact sign test on discordant pairs).",
        "",
        "| metric | alone | supervised | lower / higher |",
        "|---|---|---|---|",
    ]
    for key, label, _higher_better in ROWS:
        a = [metrics(r)[key] for r in by_arm["alone"]]
        w = [metrics(r)[key] for r in by_arm["watchman"]]
        both: list[tuple[float, float]] = []
        for p in paired:
            x, y = p["alone"][key], p["watchman"][key]
            if x is not None and y is not None:
                both.append((x, y))
        lo = sum(y < x for x, y in both)
        hi = sum(y > x for x, y in both)
        lines.append(
            f"| {label} | {avg(a)} | {avg(w)} | {lo} / {hi} (p = {sign_test(lo, hi):.2f}) |"
        )
    for arm in ("alone", "watchman"):
        ok = sum(bool(r["evaluation"]["correct"]) for r in by_arm[arm])
        lines.append(f"\n- {arm}: {ok}/{len(by_arm[arm])} runs with passing own tests.")
    sym: dict[str, dict[str, int]] = {"alone": defaultdict(int), "watchman": defaultdict(int)}
    for r in done:
        for k, v in r["style_metrics"]["pylint_by_symbol"].items():
            sym[r["arm"]][k] += v
    top = sorted(
        {k for a in sym.values() for k in a}, key=lambda k: -(sym["alone"][k] + sym["watchman"][k])
    )
    if top:
        lines += ["", "Most common pylint messages (total over runs):", ""]
        lines += ["| message | alone | supervised |", "|---|---|---|"]
        lines += [f"| {k} | {sym['alone'][k]} | {sym['watchman'][k]} |" for k in top[:8]]
    return "\n".join(lines) + "\n"


@app.command()
def main() -> None:
    files = sorted(STYLE_DIR.glob("*.json"))
    if not files:
        typer.echo("no experiment 3 results yet", err=True)
        raise typer.Exit(1)
    datasets = [json.loads(f.read_text()) for f in files]
    merged = merge(datasets)
    merged["style"] = datasets[-1].get("style", "?")
    out = RESULTS / "STYLE.md"
    out.write_text("# Experiment 3: Google-style supervision\n\n" + render(merged))
    typer.echo(f"wrote {out}")


if __name__ == "__main__":
    app()
