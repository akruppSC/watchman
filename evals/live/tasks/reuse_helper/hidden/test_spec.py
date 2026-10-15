import app.models
import app.text
from app.models import Article


def test_slug_of_an_accented_title():
    assert Article("Héllo, World!", "").url_slug == "hello-world"


def test_slug_collapses_punctuation():
    assert Article("  Rock & Roll -- Forever!! ", "").url_slug == "rock-roll-forever"


def test_the_existing_helper_is_reused(monkeypatch):
    monkeypatch.setattr(app.text, "slugify", lambda *a, **k: "SENTINEL")
    if hasattr(app.models, "slugify"):
        monkeypatch.setattr(app.models, "slugify", lambda *a, **k: "SENTINEL")
    assert Article("anything", "").url_slug == "SENTINEL"
