"""Match findings to ground truth and aggregate metrics.

Matching rule: a finding matches a defect if it names the same file and its line range
overlaps the defect's range, with a tolerance of LINE_TOLERANCE lines. Each defect matches
at most one finding, greedily by confidence. Category agreement is tracked separately, so
the results show both "found the right spot" (lenient) and "found it and named it
correctly" (strict).

On defect items, unmatched findings count as false positives. That makes precision
pessimistic, because a real but unlabeled issue also counts against it. The clean set (C)
gives the uncontaminated false-positive measure: findings per clean diff.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from pydantic import BaseModel

from evals.dataset import Defect, Item, ItemKind
from watchman_review.schema import Category, Finding, ReviewResult

LINE_TOLERANCE = 3


class Match(BaseModel):
    defect_index: int
    finding_id: str
    category_match: bool


class ItemScore(BaseModel):
    item_id: str
    kind: ItemKind
    n_defects: int
    n_findings: int
    matches: list[Match]
    false_positive_ids: list[str]
    cost_usd: float
    latency_s: float
    tool_calls: int
    error: str | None = None


def overlaps(f: Finding, d: Defect, tol: int = LINE_TOLERANCE) -> bool:
    for r in d.ranges():
        if f.file != r.file:
            continue
        if f.line_start == 0:  # file-level finding: matches any defect in that file
            return True
        if f.line_start <= r.line_end + tol and f.line_end >= r.line_start - tol:
            return True
    return False


def score_item(item: Item, result: ReviewResult) -> ItemScore:
    findings = sorted(result.findings, key=lambda f: -f.confidence)
    used: set[str] = set()
    matches: list[Match] = []
    for di, d in enumerate(item.defects):
        candidates = [f for f in findings if f.id not in used and overlaps(f, d)]
        if not candidates:
            continue
        same_cat = [f for f in candidates if f.category is d.category]
        best = (same_cat or candidates)[0]
        used.add(best.id)
        matches.append(Match(defect_index=di, finding_id=best.id, category_match=bool(same_cat)))
    return ItemScore(
        item_id=item.id,
        kind=item.kind,
        n_defects=len(item.defects),
        n_findings=len(result.findings),
        matches=matches,
        false_positive_ids=[f.id for f in result.findings if f.id not in used],
        cost_usd=result.usage.cost_usd,
        latency_s=result.latency_s,
        tool_calls=result.usage.tool_calls,
    )


def error_score(item: Item, error: str) -> ItemScore:
    return ItemScore(
        item_id=item.id, kind=item.kind, n_defects=len(item.defects), n_findings=0,
        matches=[], false_positive_ids=[], cost_usd=0.0, latency_s=0.0, tool_calls=0,
        error=error,
    )  # fmt: skip


@dataclass
class Summary:
    n_items: int = 0
    n_errors: int = 0
    defects: int = 0
    found_lenient: int = 0
    found_strict: int = 0
    findings_on_defect_items: int = 0
    true_positive_findings: int = 0
    clean_items: int = 0
    clean_findings: int = 0
    clean_items_with_findings: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    tool_calls: int = 0
    recall_by_category: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def recall(self) -> float | None:
        return self.found_lenient / self.defects if self.defects else None

    @property
    def recall_strict(self) -> float | None:
        return self.found_strict / self.defects if self.defects else None

    @property
    def precision(self) -> float | None:
        n = self.findings_on_defect_items
        return self.true_positive_findings / n if n else None

    @property
    def fp_per_clean_diff(self) -> float | None:
        return self.clean_findings / self.clean_items if self.clean_items else None

    @property
    def mean_cost(self) -> float:
        return self.cost_usd / self.n_items if self.n_items else 0.0

    @property
    def mean_latency(self) -> float:
        return self.latency_s / self.n_items if self.n_items else 0.0


def summarize(items: list[Item], scores: list[ItemScore]) -> Summary:
    by_id = {i.id: i for i in items}
    s = Summary()
    cat_found: dict[Category, int] = defaultdict(int)
    cat_total: dict[Category, int] = defaultdict(int)
    for sc in scores:
        s.n_items += 1
        if sc.error:
            s.n_errors += 1
        s.cost_usd += sc.cost_usd
        s.latency_s += sc.latency_s
        s.tool_calls += sc.tool_calls
        item = by_id[sc.item_id]
        if item.kind is ItemKind.CLEAN:
            s.clean_items += 1
            s.clean_findings += sc.n_findings
            s.clean_items_with_findings += sc.n_findings > 0
            continue
        s.defects += sc.n_defects
        s.found_lenient += len(sc.matches)
        s.found_strict += sum(m.category_match for m in sc.matches)
        s.findings_on_defect_items += sc.n_findings
        s.true_positive_findings += len(sc.matches)
        matched = {m.defect_index for m in sc.matches}
        for di, d in enumerate(item.defects):
            cat_total[d.category] += 1
            cat_found[d.category] += di in matched
    s.recall_by_category = {c.value: (cat_found[c], cat_total[c]) for c in cat_total}
    return s
