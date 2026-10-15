import app.models
import app.util.strings as strings
from app.models import Ticket


def test_whitespace_is_collapsed_and_trimmed():
    assert Ticket(1, "  Login   page\n is broken ").summary == "Login page is broken"


def test_long_titles_are_cut_with_an_ellipsis():
    title = "word " * 20
    out = Ticket(1, title).summary
    assert len(out) <= 40 and out.endswith("…")
    assert out == strings.truncate(strings.collapse_whitespace(title), 40)


def test_exactly_forty_characters_is_not_cut():
    title = "x" * 40
    assert Ticket(1, title).summary == title


def test_forty_one_characters_is_cut():
    out = Ticket(1, "x" * 41).summary
    assert len(out) <= 40 and out.endswith("…")


def test_the_existing_helpers_are_reused(monkeypatch):
    monkeypatch.setattr(strings, "collapse_whitespace", lambda t: "C:" + t)
    monkeypatch.setattr(strings, "truncate", lambda t, n, *a, **k: f"T({t})")
    for name, repl in (("collapse_whitespace", lambda t: "C:" + t),
                       ("truncate", lambda t, n, *a, **k: f"T({t})")):
        if hasattr(app.models, name):
            monkeypatch.setattr(app.models, name, repl)
    assert Ticket(1, "hi").summary == "T(C:hi)"
