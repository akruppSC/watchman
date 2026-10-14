import re
from dataclasses import dataclass


@dataclass
class Article:
    title: str
    body: str

    @property
    def url_slug(self) -> str:
        return make_url_slug(self.title)


def make_url_slug(title: str) -> str:
    """Turn an article title into a URL-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower())
    return slug.strip("-")
