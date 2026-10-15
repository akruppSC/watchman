from app.models import Article


def test_url_slug_strips_accents_and_punctuation():
    assert Article("Héllo, World!", "").url_slug == "hello-world"


def test_url_slug_collapses_runs():
    assert Article("Rock & Roll -- Forever!!", "").url_slug == "rock-roll-forever"
