from app.invoice import render_html


def test_small_amounts_keep_two_decimals():
    assert "<td>5.00</td>" in render_html([("tea", 5)])


def test_thousands_separator_and_two_decimals():
    assert "<td>1,234.50</td>" in render_html([("tea", 1234.5)])


def test_millions():
    assert "<td>1,000,000.00</td>" in render_html([("x", 1000000)])
