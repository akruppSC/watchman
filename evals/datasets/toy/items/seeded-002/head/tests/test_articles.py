from textkit.articles import Article


def test_url_slug() -> None:
    article = Article(title="Héllo, World!", body="...")
    assert article.url_slug is not None
