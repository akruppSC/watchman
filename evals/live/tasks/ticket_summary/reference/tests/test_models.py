from app.models import Ticket


def test_summary_collapses_whitespace():
    assert Ticket(1, "  Login   page\n broken ").summary == "Login page broken"


def test_summary_cuts_long_titles():
    out = Ticket(1, "word " * 20).summary
    assert len(out) <= 40 and out.endswith("…")


def test_summary_boundary_at_forty():
    assert Ticket(1, "x" * 40).summary == "x" * 40
    assert Ticket(1, "x" * 41).summary.endswith("…")
