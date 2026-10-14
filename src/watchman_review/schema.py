"""Findings and review-result models.

These are the contract between every layer (deterministic checks, baseline, agent),
the CLI output, and the eval harness. Keep them stable. Additive changes only.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Category(StrEnum):
    HALLUCINATED_DEPENDENCY = "hallucinated_dependency"
    WEAK_TEST = "weak_test"
    SWALLOWED_ERROR = "swallowed_error"
    DUPLICATION = "duplication"
    OVER_ABSTRACTION = "over_abstraction"
    SCOPE_CREEP = "scope_creep"
    SILENT_BEHAVIOR_CHANGE = "silent_behavior_change"
    SECURITY = "security"
    CONVENTION_VIOLATION = "convention_violation"
    COMMENT_NOISE = "comment_noise"
    CORRECTNESS = "correctness"
    OTHER = "other"


class Severity(StrEnum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {Severity.BLOCKER: 3, Severity.MAJOR: 2, Severity.MINOR: 1, Severity.NIT: 0}


class Source(StrEnum):
    DETERMINISTIC = "deterministic"
    AGENT = "agent"
    BASELINE = "baseline"


class Verdict(StrEnum):
    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"


class ReviewMode(StrEnum):
    DETERMINISTIC = "deterministic"
    BASELINE = "baseline"
    AGENT = "agent"


class Finding(BaseModel):
    """A single review finding. Every finding must carry evidence."""

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    category: Category
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    file: str | None = Field(
        default=None, description="Repo-relative path, or null for change-level findings."
    )
    line_start: int = Field(default=0, ge=0, description="1-based; 0 means file/change-level.")
    line_end: int = Field(default=0, ge=0)
    summary: str = Field(min_length=1, max_length=300)
    evidence: str = Field(min_length=1)
    suggested_fix: str | None = None
    source: Source
    rule: str | None = Field(
        default=None, description="Deterministic rule id, e.g. 'ruff:F401' or 'secrets:aws'."
    )
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="IDs of tool calls / check results backing this finding (grounding).",
    )

    @field_validator("summary", "evidence")
    @classmethod
    def _strip_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        if self.line_end < self.line_start:
            self.line_end = self.line_start
        if not self.id:
            self.id = self.fingerprint()
        return self

    def fingerprint(self) -> str:
        """Stable identity used for dedupe across sources and review rounds.

        Ignores severity, confidence, and wording of evidence so that the same issue
        reported twice collapses to one ID.
        """
        key = f"{self.category}|{self.file or ''}|{self.line_start}|{self.rule or self.summary}"
        return hashlib.sha256(key.encode()).hexdigest()[:12]


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class CheckResult(BaseModel):
    """Outcome of one deterministic check. This is also fed to the agent as evidence."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable reference, e.g. 'check:ruff'. Cited in evidence_refs.")
    name: str
    status: CheckStatus
    summary: str = ""
    duration_s: float = 0.0
    findings: list[Finding] = Field(default_factory=list)
    output_excerpt: str = Field(default="", description="Truncated raw tool output.")


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    llm_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: Usage) -> None:
        self.llm_calls += other.llm_calls
        self.tool_calls += other.tool_calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd += other.cost_usd
        if other.model and not self.model:
            self.model = other.model


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    mode: ReviewMode
    base: str
    head: str
    verdict: Verdict
    findings: list[Finding]
    checks: list[CheckResult] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    latency_s: float = 0.0
    dropped_findings: int = Field(
        default=0, description="Findings removed by grounding/threshold filters."
    )
    warnings: list[str] = Field(default_factory=list)


def compute_verdict(findings: list[Finding], *, block_min_confidence: float = 0.7) -> Verdict:
    """Derive the overall verdict.

    - block: any blocker with confidence >= block_min_confidence
    - warn:  any major or minor finding (or a low-confidence blocker)
    - pass:  nothing, or nits only
    """
    verdict = Verdict.PASS
    for f in findings:
        if f.severity is Severity.BLOCKER and f.confidence >= block_min_confidence:
            return Verdict.BLOCK
        if f.severity is not Severity.NIT:
            verdict = Verdict.WARN
    return verdict


def dedupe(findings: list[Finding]) -> list[Finding]:
    """Collapse findings with the same fingerprint, keeping the most severe/confident."""
    best: dict[str, Finding] = {}
    for f in findings:
        cur = best.get(f.id)
        if cur is None or (f.severity.rank, f.confidence) > (cur.severity.rank, cur.confidence):
            best[f.id] = f
    return sorted(
        best.values(),
        key=lambda f: (-f.severity.rank, f.file or "", f.line_start, f.id),
    )
