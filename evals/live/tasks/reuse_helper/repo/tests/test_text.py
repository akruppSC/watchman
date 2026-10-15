from app.text import slugify


def test_slugify_strips_accents_and_punctuation():
    assert slugify("Héllo, World!") == "hello-world"


def test_slugify_truncates():
    assert slugify("a" * 100, max_length=10) == "a" * 10
