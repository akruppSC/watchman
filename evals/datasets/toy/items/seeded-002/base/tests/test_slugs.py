from textkit.slugs import slugify


def test_slugify_strips_accents_and_punctuation() -> None:
    assert slugify("Héllo, World!") == "hello-world"


def test_slugify_truncates() -> None:
    assert slugify("a" * 100, max_length=10) == "a" * 10
