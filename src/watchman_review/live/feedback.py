"""What the worker sees: terse, structured, and cheap on context.

A review can produce pages of text, but the worker pays for every token it reads, so the
feedback is a header line plus one line per finding, new findings first, and findings the
worker has already seen collapse to a short "still open" line instead of being explained again.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Status = Literal["clean", "open", "escalated", "unchanged", "error", "nothing"]
Gate = Literal["pass", "block", "released"]


class LiveFinding(BaseModel):
    key: str
    state: Literal["new", "open"]
    severity: str
    category: str
    location: str
    summary: str
    fix: str | None = None
    ref: str | None = None
    since_round: int = 0


class Feedback(BaseModel):
    kind: str
    status: Status
    round: int = 0
    cap: int = 3
    verdict: str = "pass"
    gate: Gate = "pass"
    policy: str = "advisory"
    new: list[LiveFinding] = Field(default_factory=list)
    open: list[LiveFinding] = Field(default_factory=list)
    fixed: int = 0
    hidden: int = 0  # findings left out of the text to keep it short
    nits: int = 0
    snapshot: str = ""
    base: str = ""
    cost_usd: float = 0.0
    message: str = ""

    def render(self) -> str:
        head = f"WATCHMAN {self.kind}"
        if self.status == "nothing":
            return f"{head}: nothing to review ({self.message or 'no changes against the base'})."
        if self.status == "error":
            return f"{head}: review did not complete ({self.message}). No action needed from you."
        if self.status == "clean":
            extra = f", {self.fixed} fixed since the last round" if self.fixed else ""
            nit = f", {self.nits} nit(s) not shown" if self.nits else ""
            return f"{head}: clean (verdict={self.verdict.upper()}{extra}{nit})."
        lines = [
            f"{head} round {self.round}/{self.cap} verdict={self.verdict.upper()} "
            f"gate={self.gate} new={len(self.new)} "
            f"open={len(self.open)} fixed={self.fixed}"
        ]
        if self.status == "unchanged":
            lines[0] += " (no changes since the last review)"
        for f in self.new:
            lines.append(self._line("+", f))
        for f in self.open:
            lines.append(self._line("=", f))
        if self.hidden:
            lines.append(f"(+{self.hidden} more; `watchman live status` lists them)")
        if self.status == "escalated":
            lines.append(
                f"ESCALATED after {self.cap} rounds: stop editing for these findings. Summarize "
                "what is still open for the human and wait for their decision."
            )
        elif self.gate == "block":
            lines.append("BLOCKED: fix the blocker/major items above, then retry.")
        elif self.message:
            lines.append(self.message)
        return "\n".join(lines)

    @staticmethod
    def _line(mark: str, f: LiveFinding) -> str:
        since = f" (open since r{f.since_round})" if f.state == "open" and f.since_round else ""
        fix = f" | fix: {f.fix}" if f.fix and f.state == "new" else ""
        ref = f" [{f.ref}]" if f.ref else ""
        return f"{mark} {f.severity} {f.category} {f.location}{since} {f.summary}{fix}{ref}"
