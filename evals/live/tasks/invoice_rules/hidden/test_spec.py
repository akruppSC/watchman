import pytest
from app.invoices import validate_invoice


def good(**kw):
    inv = {"id": "A1", "customer": "Acme", "currency": "EUR", "issued": "2026-03-01",
           "due": "2026-03-31", "lines": [{"sku": "s1", "qty": 2, "price": 10.0},
                                          {"sku": "s2", "qty": 1, "price": 0}]}
    inv.update(kw)
    return inv


def test_valid():
    assert validate_invoice(good()) == []
    assert validate_invoice(good(due="2026-03-01")) == []


@pytest.mark.parametrize("field,value,msg", [
    ("id", "", "missing id"), ("id", "  ", "missing id"), ("id", None, "missing id"),
    ("customer", " ", "missing customer"), ("customer", 5, "missing customer"),
    ("currency", "JPY", "unsupported currency"), ("currency", None, "unsupported currency"),
    ("issued", "2026-13-01", "bad issue date"), ("issued", "2026-02-30", "bad issue date"),
    ("issued", "03/01/2026", "bad issue date"), ("issued", None, "bad issue date"),
    ("due", "tomorrow", "bad due date"), ("due", 20260331, "bad due date"),
    ("notes", "x" * 501, "notes too long"),
])
def test_single_field_errors(field, value, msg):
    assert validate_invoice(good(**{field: value})) == [msg]


def test_missing_keys():
    inv = good()
    for key in ("id", "customer", "currency", "issued", "due"):
        del inv[key]
    assert validate_invoice(inv) == [
        "missing id", "missing customer", "unsupported currency", "bad issue date", "bad due date"]


def test_notes_boundary():
    assert validate_invoice(good(notes="x" * 500)) == []


def test_due_before_issued_only_when_both_dates_are_valid():
    assert validate_invoice(good(due="2026-02-28")) == ["due before issued"]
    assert validate_invoice(good(due="nope", issued="2026-03-01")) == ["bad due date"]


def test_no_lines():
    assert validate_invoice(good(lines=[])) == ["no lines"]
    inv = good()
    del inv["lines"]
    assert validate_invoice(inv) == ["no lines"]
    assert validate_invoice(good(lines=None)) == ["no lines"]


def test_line_errors_in_order():
    lines = [{"sku": "", "qty": 1.5, "price": "3"}, {"sku": "ok", "qty": True, "price": True},
             {"sku": "fine", "qty": 3, "price": 1}]
    assert validate_invoice(good(lines=lines)) == [
        "line 1: missing sku", "line 1: bad quantity", "line 1: bad price",
        "line 2: bad quantity", "line 2: bad price"]


def test_negative_and_zero():
    assert validate_invoice(good(lines=[{"sku": "a", "qty": -1, "price": 1}])) == ["line 1: bad quantity"]
    assert validate_invoice(good(lines=[{"sku": "a", "qty": 1, "price": -0.01}])) == ["line 1: bad price"]
    assert validate_invoice(good(lines=[{"sku": "a", "qty": 1, "price": 0}])) == []


def test_duplicates_reported_once_after_line_errors():
    lines = [{"sku": "a", "qty": 1, "price": 1}, {"sku": "b", "qty": 0, "price": 1},
             {"sku": "a", "qty": 1, "price": 1}, {"sku": "a", "qty": 1, "price": 1},
             {"sku": "b", "qty": 1, "price": 1}]
    assert validate_invoice(good(lines=lines)) == [
        "line 2: bad quantity", "duplicate sku: a", "duplicate sku: b"]


def test_missing_skus_are_not_duplicates():
    lines = [{"qty": 1, "price": 1}, {"qty": 1, "price": 1}]
    assert validate_invoice(good(lines=lines)) == ["line 1: missing sku", "line 2: missing sku"]


@pytest.mark.parametrize("rate,ok", [(0, True), (0.5, True), (0.2, True), (-0.01, False),
                                     (0.51, False), ("0.1", False), (True, False), (None, False)])
def test_tax_rate(rate, ok):
    assert validate_invoice(good(tax_rate=rate)) == ([] if ok else ["bad tax rate"])


def test_total_checks():
    assert validate_invoice(good(total=20.0)) == []
    assert validate_invoice(good(total=21.0)) == ["total mismatch"]
    assert validate_invoice(good(tax_rate=0.1, total=22.0)) == []
    assert validate_invoice(good(tax_rate=0.07, total=21.4)) == []
    assert validate_invoice(good(tax_rate=0.1, total=20.0)) == ["total mismatch"]


def test_total_is_skipped_when_lines_or_tax_are_invalid():
    bad_line = [{"sku": "a", "qty": 0, "price": 1}]
    assert validate_invoice(good(lines=bad_line, total=99)) == ["line 1: bad quantity"]
    assert validate_invoice(good(tax_rate=9, total=99)) == ["bad tax rate"]
    assert validate_invoice(good(lines=[], total=99)) == ["no lines"]


def test_errors_keep_rule_order():
    inv = good(id="", currency="x", lines=[{"sku": "a", "qty": 0, "price": 1}] * 2,
               tax_rate=2, notes="n" * 600)
    assert validate_invoice(inv) == [
        "missing id", "unsupported currency", "line 1: bad quantity", "line 2: bad quantity",
        "duplicate sku: a", "bad tax rate", "notes too long"]
