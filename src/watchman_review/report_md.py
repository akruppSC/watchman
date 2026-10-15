"""Markdown rendering of a ReviewResult, for PR comments and CI step summaries.

Everything a model wrote (summaries, evidence, notes) was produced after reading untrusted
code, and a PR comment is public and posted under a bot identity. So all of it is sanitized:
HTML is escaped, @mentions and #refs can't ping or link, markdown links and bare URLs can't
become clickable, and nothing can forge the marker that identifies our comment.
"""

from __future__ import annotations

import re
from collections import Counter

from watchman_review.schema import (
    CheckStatus,
    Finding,
    ReviewResult,
    Severity,
    StageSummary,
    Verdict,
)

MARKER = "<!-- watchman-review -->"
MAX_FINDINGS_SHOWN = 25
MAX_COMMENT_CHARS = 60_000  # GitHub's hard limit is 65,536
ZWSP = "\u200b"

_VERDICT_TEXT = {
    Verdict.PASS: "PASS: nothing blocks this change",
    Verdict.WARN: "WARN: findings worth a look",
    Verdict.BLOCK: "BLOCK: at least one finding should stop a merge",
}
_URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*)://")
_MENTION = re.compile(r"@(?=[\w-])")
_ISSUE_REF = re.compile(r"(?<![\w&])#(?=\d)")
_FENCE = re.compile(r"`{3,}")


def md_safe(text: str, limit: int | None = None) -> str:
    """Make untrusted text inert inside Markdown without hiding what it says."""
    text = text.replace("\r", "")
    text = _FENCE.sub("`", text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("[", "\\[").replace("]", "\\]")  # no [text](url) links
    text = _URL.sub(lambda m: f"{m.group(1)}:{ZWSP}//", text)  # no autolinked URLs
    text = _MENTION.sub(f"@{ZWSP}", text)  # no pings
    text = _ISSUE_REF.sub(f"#{ZWSP}", text)  # no cross-reference spam
    text = text.replace("|", "\\|")
    if limit is not None and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def code_safe(text: str) -> str:
    """For inline code spans: no backticks or newlines that could end the span."""
    return text.replace("`", "'").replace("\n", " ").replace("\r", "")


def _location(f: Finding) -> str:
    if not f.file:
        return "(whole change)"
    if not f.line_start:
        return f.file
    end = f"-{f.line_end}" if f.line_end > f.line_start else ""
    return f"{f.file}:{f.line_start}{end}"


def _link(f: Finding, blob_base: str | None) -> str:
    loc = code_safe(_location(f))
    if not blob_base or not f.file:
        return f"`{loc}`"
    frag = ""
    if f.line_start:
        frag = f"#L{f.line_start}" + (f"-L{f.line_end}" if f.line_end > f.line_start else "")
    # file paths come from the diff, but treat them as untrusted too
    path = re.sub(r"[^A-Za-z0-9_./\-]", "", f.file)
    return f"[`{loc}`]({blob_base.rstrip('/')}/{path}{frag})"


def _finding_md(f: Finding, blob_base: str | None) -> str:
    tag = f.source.value + (f", {f.verified_by}" if f.verified_by else "")
    head = (
        f"- **{f.severity.value}** · {f.category.value} · {_link(f, blob_base)} "
        f"_(confidence {f.confidence:.2f}, {tag})_"
    )
    lines = [head, f"  {md_safe(f.summary, 300)}"]
    if f.evidence:
        lines.append(f"  > {md_safe(f.evidence, 400)}")
    if f.suggested_fix:
        lines.append(f"  Fix: {md_safe(f.suggested_fix, 300)}")
    return "\n".join(lines)


def _counts(findings: list[Finding]) -> str:
    c = Counter(f.severity for f in findings)
    parts = [f"{c[s]} {s.value}" for s in Severity if c[s]]
    return ", ".join(parts) if parts else "no findings"


def _stages_table(stages: list[StageSummary]) -> str:
    rows = ["| stage | model | status | cost | confirmed | rejected | downgraded | added |",
            "|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for s in stages:
        if s.error:
            status = f"failed: {md_safe(s.error, 80)}"
        elif not s.ran:
            status = "skipped"
        else:
            status = md_safe("; ".join(s.trigger_reasons) or "ran", 120)
        rows.append(
            f"| {s.name} | {md_safe(s.model or '-')} | {status} | ${s.usage.cost_usd:.4f} | "
            f"{s.confirmed} | {s.rejected} | {s.downgraded} | {s.added} |"
        )
    return "\n".join(rows)


def render_markdown(result: ReviewResult, *, blob_base: str | None = None) -> str:
    """The PR comment body. Starts with MARKER so a later run can update it in place."""
    out = [MARKER, f"## Watchman review: **{_VERDICT_TEXT[result.verdict]}**", ""]
    if result.incomplete:
        out += [
            "> **Incomplete review.** "
            + md_safe("; ".join(result.incomplete_reasons))
            + ". Findings may be missing, so this is not a clean bill of health.",
            "",
        ]
    u = result.usage
    summary = f"{len(result.findings)} finding(s): {_counts(result.findings)}. "
    summary += f"Mode `{result.mode.value}`"
    if u.llm_calls:
        summary += f", model `{code_safe(u.model or '-')}`, ${u.cost_usd:.3f}"
    out += [summary + f", {result.latency_s:.0f}s.", ""]

    ordered = sorted(
        result.findings, key=lambda f: (-f.severity.rank, -f.confidence, f.file or "", f.line_start)
    )
    for f in ordered[:MAX_FINDINGS_SHOWN]:
        out += [_finding_md(f, blob_base), ""]
    if len(ordered) > MAX_FINDINGS_SHOWN:
        out += [f"…and {len(ordered) - MAX_FINDINGS_SHOWN} more (see the JSON artifact).", ""]

    if result.checks:
        rows = ["| check | status | summary |", "|---|---|---|"]
        for c in result.checks:
            rows.append(f"| {c.name} | {c.status.value} | {md_safe(c.summary, 160)} |")
        failed = [c for c in result.checks if c.status is CheckStatus.FAILED]
        label = f"Layer 1 checks ({len(failed)} found problems)" if failed else "Layer 1 checks"
        out += [f"<details><summary>{label}</summary>", "", *rows, "", "</details>", ""]
    if any(s.name != "primary" for s in result.stages):
        out += ["<details><summary>Review stages</summary>", "", _stages_table(result.stages),
                "", "</details>", ""]  # fmt: skip
    if result.dropped:
        out += [f"{len(result.dropped)} candidate finding(s) were filtered out as unverified or "
                "duplicate (listed in the JSON output).", ""]  # fmt: skip
    notes = [w for w in result.warnings if "subprocess" not in w.lower()]
    if notes:
        out += [
            "<details><summary>Warnings</summary>",
            "",
            *[f"- {md_safe(w, 300)}" for w in notes[:10]],
            "",
            "</details>",
            "",
        ]
    out.append(
        "<sub>Posted by Watchman. Every finding cites evidence; see the JSON for details.</sub>"
    )
    body = "\n".join(out)
    if len(body) > MAX_COMMENT_CHARS:
        body = body[: MAX_COMMENT_CHARS - 80].rstrip() + "\n\n…(comment truncated)\n"
    return body
