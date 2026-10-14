from textkit.articles import Article


def test_url_slug_uses_title() -> None:
    assert Article(title="Héllo, World!", body="...").url_slug == "hello-world"
