from app.export import to_csv
from app.invoice import render_html
from app.money import format_price


def cell(html):
    return html.split("<td>")[2].split("</td>")[0]


def test_thousands_separator():
    assert cell(render_html([("a", 1234.5)])) == "1,234.50"


def test_small_amounts_are_unchanged():
    assert cell(render_html([("a", 5)])) == "5.00"
    assert cell(render_html([("a", 999.9)])) == "999.90"


def test_millions_and_negatives():
    assert cell(render_html([("a", 1000000)])) == "1,000,000.00"
    assert cell(render_html([("a", -1234.5)])) == "-1,234.50"


# Everything outside the HTML invoice must behave exactly as before.
def test_csv_export_is_unchanged():
    assert to_csv([("a", 1234.5), ("b", 7)]) == "a,1234.50\nb,7.00"


def test_format_price_is_unchanged():
    assert format_price(1234.5) == "1234.50"
    assert format_price(1000000) == "1000000.00"
