"""Invoice validation: one small function per rule, applied in order."""

from datetime import date

CURRENCIES = {"USD", "EUR", "GBP"}


def blank(value):
    return not isinstance(value, str) or not value.strip()


def parse_date(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        return None


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def header_errors(inv):
    errors = []
    if blank(inv.get("id")):
        errors.append("missing id")
    if blank(inv.get("customer")):
        errors.append("missing customer")
    if inv.get("currency") not in CURRENCIES:
        errors.append("unsupported currency")
    return errors


def date_errors(inv):
    issued, due = parse_date(inv.get("issued")), parse_date(inv.get("due"))
    errors = []
    if issued is None:
        errors.append("bad issue date")
    if due is None:
        errors.append("bad due date")
    if issued and due and due < issued:
        errors.append("due before issued")
    return errors


def line_errors(n, line):
    errors = []
    if blank(line.get("sku")):
        errors.append(f"line {n}: missing sku")
    qty = line.get("qty")
    if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
        errors.append(f"line {n}: bad quantity")
    price = line.get("price")
    if not is_number(price) or price < 0:
        errors.append(f"line {n}: bad price")
    return errors


def duplicate_errors(lines):
    seen, reported, errors = set(), set(), []
    for line in lines:
        sku = line.get("sku")
        if blank(sku):
            continue
        if sku in seen and sku not in reported:
            reported.add(sku)
            errors.append(f"duplicate sku: {sku}")
        seen.add(sku)
    return errors


def tax_error(inv):
    if "tax_rate" not in inv:
        return []
    rate = inv["tax_rate"]
    return [] if is_number(rate) and 0 <= rate <= 0.5 else ["bad tax rate"]


def total_error(inv, lines_ok, tax_ok):
    if "total" not in inv or not (lines_ok and tax_ok):
        return []
    net = sum(line["qty"] * line["price"] for line in inv["lines"])
    expected = round(net * (1 + inv.get("tax_rate", 0)), 2)
    return [] if inv["total"] == expected else ["total mismatch"]


def validate_invoice(invoice):
    lines = invoice.get("lines")
    errors = header_errors(invoice) + date_errors(invoice)
    if not isinstance(lines, list) or not lines:
        errors.append("no lines")
        lines = []
    per_line = [line_errors(n, line) for n, line in enumerate(lines, 1)]
    errors += [e for group in per_line for e in group]
    errors += duplicate_errors(lines)
    tax = tax_error(invoice)
    errors += tax
    errors += total_error(invoice, bool(lines) and not any(per_line), not tax)
    if len(str(invoice.get("notes", ""))) > 500:
        errors.append("notes too long")
    return errors
