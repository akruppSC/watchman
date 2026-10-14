import pytest
from pydantic import ValidationError

from watchman_review.schema import (
    Category,
    Finding,
    ReviewMode,
    ReviewResult,
    Severity,
    Source,
    Verdict,
    compute_verdict,
    dedupe,
)


def make(**kw: object) -> Finding:
    base: dict[str, object] = {
        "category": Category.CORRECTNESS,
        "severity": Severity.MAJOR,
        "confidence": 0.9,
        "file": "a.py",
        "line_start": 3,
        "line_end": 4,
        "summary": "Off-by-one in loop bound",
        "evidence": "read_file a.py:3-4 shows range(len(x)+1)",
        "source": Source.AGENT,
    }
    base.update(kw)
    return Finding.model_validate(base)


def test_id_is_derived_and_stable() -> None:
    a = make()
    b = make(severity=Severity.MINOR, confidence=0.4, evidence="different wording")
    assert a.id == b.id
    assert len(a.id) == 12


def test_id_differs_by_location() -> None:
    assert make().id != make(line_start=10, line_end=10).id


def test_blank_evidence_rejected() -> None:
    with pytest.raises(ValidationError):
        make(evidence="   ")


def test_confidence_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        make(confidence=1.5)


def test_unknown_category_rejected() -> None:
    with pytest.raises(ValidationError):
        make(category="vibes")


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        make(made_up_field=1)


def test_line_end_normalized_to_start() -> None:
    f = make(line_start=7, line_end=2)
    assert f.line_end == 7


@pytest.mark.parametrize(
    ("findings", "expected"),
    [
        ([], Verdict.PASS),
        ([make(severity=Severity.NIT)], Verdict.PASS),
        ([make(severity=Severity.MINOR)], Verdict.WARN),
        ([make(severity=Severity.BLOCKER, confidence=0.5)], Verdict.WARN),
        (
            [make(severity=Severity.NIT), make(severity=Severity.BLOCKER, line_start=9)],
            Verdict.BLOCK,
        ),
    ],
)
def test_compute_verdict(findings: list[Finding], expected: Verdict) -> None:
    assert compute_verdict(findings) is expected


def test_dedupe_keeps_most_severe() -> None:
    weak = make(severity=Severity.MINOR, confidence=0.3)
    strong = make(severity=Severity.BLOCKER, confidence=0.8)
    other = make(line_start=50, line_end=50, severity=Severity.NIT)
    out = dedupe([weak, other, strong])
    assert [f.severity for f in out] == [Severity.BLOCKER, Severity.NIT]


def test_review_result_roundtrips_json() -> None:
    r = ReviewResult(
        mode=ReviewMode.AGENT, base="main", head="HEAD", verdict=Verdict.WARN, findings=[make()]
    )
    again = ReviewResult.model_validate_json(r.model_dump_json())
    assert again == r
