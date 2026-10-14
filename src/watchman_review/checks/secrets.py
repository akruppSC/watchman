"""Secret scan over lines added by the diff.

Matched values are always redacted in findings so the review output (which may be posted
as a PR comment) never re-leaks the secret.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from watchman_review.checks.base import Check, CheckContext
from watchman_review.schema import Category, CheckResult, CheckStatus, Finding, Severity, Source


@dataclass(frozen=True)
class Rule:
    id: str
    description: str
    pattern: re.Pattern[str]
    severity: Severity = Severity.BLOCKER
    confidence: float = 0.95
    group: int = 0


RULES: list[Rule] = [
    Rule(
        "aws-access-key",
        "AWS access key ID",
        re.compile(r"\b((?:AKIA|ASIA)[0-9A-Z]{16})\b"),
        group=1,
    ),
    Rule(
        "github-token", "GitHub token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,255})\b"), group=1
    ),
    Rule(
        "github-pat",
        "GitHub fine-grained PAT",
        re.compile(r"\b(github_pat_[A-Za-z0-9_]{60,})\b"),
        group=1,
    ),
    Rule(
        "anthropic-key", "Anthropic API key", re.compile(r"\b(sk-ant-[A-Za-z0-9_\-]{20,})"), group=1
    ),
    Rule(
        "openrouter-key", "OpenRouter API key", re.compile(r"\b(sk-or-v1-[a-f0-9]{32,})"), group=1
    ),
    Rule(
        "openai-key",
        "OpenAI API key",
        re.compile(r"\b(sk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{32,})"),
        group=1,
    ),
    Rule("slack-token", "Slack token", re.compile(r"\b(xox[abposr]-[A-Za-z0-9-]{10,})"), group=1),
    Rule("google-api-key", "Google API key", re.compile(r"\b(AIza[0-9A-Za-z_\-]{35})\b"), group=1),
    Rule(
        "stripe-live",
        "Stripe live key",
        re.compile(r"\b((?:sk|rk)_live_[0-9a-zA-Z]{24,})"),
        group=1,
    ),
    Rule(
        "private-key", "Private key block", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")
    ),
    Rule(
        "jwt",
        "JSON Web Token",
        re.compile(r"\b(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"),
        severity=Severity.MAJOR,
        confidence=0.8,
        group=1,
    ),
    Rule(
        "url-credentials",
        "Credentials embedded in URL",
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@'\"]+:([^\s@/'\"]{6,})@[^\s'\"]+"),
        severity=Severity.MAJOR,
        confidence=0.85,
        group=1,
    ),
]

_GENERIC = re.compile(
    r"""(?ix)
    \b[\w.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|auth)[\w.-]*\b
    \s*[:=]\s*
    (?P<q>['"])(?P<val>[^'"\s]{12,})(?P=q)
    """
)
_PLACEHOLDER = re.compile(
    r"(?i)(x{4,}|\*{3,}|example|placeholder|changeme|dummy|fake|test|sample|your[_-]|<|\$\{|\{\{|redacted)"
)


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def redact(value: str) -> str:
    return value[:4] + "…" + f"[{len(value)} chars redacted]" if len(value) > 4 else "[redacted]"


def scan_line(line: str) -> list[tuple[Rule, str]]:
    hits: list[tuple[Rule, str]] = []
    for rule in RULES:
        m = rule.pattern.search(line)
        if m:
            hits.append((rule, m.group(rule.group)))
    if not hits:
        m = _GENERIC.search(line)
        if m:
            val = m.group("val")
            if not _PLACEHOLDER.search(val) and shannon_entropy(val) >= 3.5:
                hits.append(
                    (
                        Rule(
                            "generic-secret",
                            "High-entropy secret assignment",
                            _GENERIC,
                            severity=Severity.MAJOR,
                            confidence=0.7,
                        ),
                        val,
                    )
                )
    return hits


_SKIP_SUFFIXES = (".lock", ".min.js", ".svg", ".map")


class SecretsCheck(Check):
    name = "secrets"

    def run(self, ctx: CheckContext) -> CheckResult:
        findings: list[Finding] = []
        for f in ctx.diff.files:
            if f.is_binary or f.path.endswith(_SKIP_SUFFIXES):
                continue
            for lineno, text in sorted(f.added.items()):
                for rule, value in scan_line(text):
                    findings.append(
                        Finding(
                            category=Category.SECURITY,
                            severity=rule.severity,
                            confidence=rule.confidence,
                            file=f.path,
                            line_start=lineno,
                            line_end=lineno,
                            summary=f"Possible hardcoded secret: {rule.description}",
                            evidence=(
                                f"secret scan rule '{rule.id}' matched an added line at "
                                f"{f.path}:{lineno} (value {redact(value)})."
                            ),
                            suggested_fix="Remove it, rotate the credential, and load it from "
                            "an environment variable or secret store.",
                            source=Source.DETERMINISTIC,
                            rule=f"secrets:{rule.id}",
                        )
                    )
        status = CheckStatus.FAILED if findings else CheckStatus.PASSED
        return self.result(status, f"{len(findings)} possible secret(s) in added lines", findings)
