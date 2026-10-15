from app.invoices import validate_invoice


def good(**kw):
    inv = {"id": "A1", "customer": "Acme", "currency": "USD", "issued": "2026-01-10",
           "due": "2026-02-10", "lines": [{"sku": "x", "qty": 2, "price": 5.0}]}
    inv.update(kw)
    return inv


def test_valid_invoice_has_no_errors():
    assert validate_invoice(good()) == []


def test_header_and_dates():
    errs = validate_invoice(good(id=" ", currency="JPY", due="2025-01-01"))
    assert errs == ["missing id", "unsupported currency", "due before issued"]


def test_lines_and_duplicates():
    lines = [{"sku": "x", "qty": 0, "price": 1}, {"sku": "x", "qty": 1, "price": -1}]
    assert validate_invoice(good(lines=lines)) == [
        "line 1: bad quantity", "line 2: bad price", "duplicate sku: x"]


def test_total_and_tax():
    assert validate_invoice(good(total=10.0)) == []
    assert validate_invoice(good(total=11.0)) == ["total mismatch"]
    assert validate_invoice(good(tax_rate=0.9)) == ["bad tax rate"]
