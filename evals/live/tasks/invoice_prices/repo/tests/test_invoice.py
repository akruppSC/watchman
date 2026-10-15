from app.invoice import render_html


def test_render_html_small_amounts():
    assert render_html([("tea", 5)]) == "<table><tr><td>tea</td><td>5.00</td></tr></table>"
