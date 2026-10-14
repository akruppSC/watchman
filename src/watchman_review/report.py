"""Human-readable terminal rendering of a ReviewResult."""

from __future__ import annotations

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from watchman_review.schema import CheckStatus, ReviewResult, Severity, Verdict

_SEV_STYLE = {
    Severity.BLOCKER: "bold red",
    Severity.MAJOR: "red",
    Severity.MINOR: "yellow",
    Severity.NIT: "dim",
}
_VERDICT_STYLE = {
    Verdict.PASS: "bold green",
    Verdict.WARN: "bold yellow",
    Verdict.BLOCK: "bold red",
}
_CHECK_STYLE = {
    CheckStatus.PASSED: "green",
    CheckStatus.FAILED: "red",
    CheckStatus.SKIPPED: "dim",
    CheckStatus.ERROR: "magenta",
}


def render(result: ReviewResult, console: Console) -> None:
    if result.checks:
        t = Table(title="Deterministic checks", show_edge=False, title_justify="left")
        t.add_column("check")
        t.add_column("status")
        t.add_column("summary", overflow="fold")
        t.add_column("time", justify="right")
        for c in result.checks:
            t.add_row(
                c.name,
                Text(c.status.value, style=_CHECK_STYLE[c.status]),
                c.summary,
                f"{c.duration_s:.1f}s",
            )
        console.print(t)
        console.print()

    if not result.findings:
        console.print("[green]No findings.[/green]")
    for f in result.findings:
        loc = f.file or "(change)"
        if f.line_start:
            loc += f":{f.line_start}" + (f"-{f.line_end}" if f.line_end > f.line_start else "")
        header = Text.assemble(
            (f"{f.severity.value.upper():7}", _SEV_STYLE[f.severity]),
            "  ",
            (f.category.value, "cyan"),
            "  ",
            (loc, "bold"),
            (f"  conf {f.confidence:.2f}  [{f.source.value}]", "dim"),
        )
        body = [Text(f.summary), Text("evidence: " + f.evidence, style="dim")]
        if f.suggested_fix:
            body.append(Text("fix: " + f.suggested_fix, style="green"))
        console.print(Panel(Group(*body), title=header, title_align="left", expand=True))

    for w in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {w}")

    u = result.usage
    stats = f"mode={result.mode.value}  findings={len(result.findings)}"
    if result.dropped_findings:
        stats += f"  dropped={result.dropped_findings}"
    if u.llm_calls:
        stats += (
            f"  llm_calls={u.llm_calls}  tool_calls={u.tool_calls}  "
            f"tokens={u.input_tokens}+{u.output_tokens}  cost=${u.cost_usd:.4f}"
        )
    stats += f"  latency={result.latency_s:.1f}s"
    console.print(Text(stats, style="dim"))
    console.print(
        Text.assemble("verdict: ", (result.verdict.value.upper(), _VERDICT_STYLE[result.verdict]))
    )
