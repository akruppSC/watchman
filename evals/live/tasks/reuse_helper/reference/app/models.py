from dataclasses import dataclass

from app.text import slugify


@dataclass
class Article:
    title: str
    body: str

    @property
    def url_slug(self) -> str:
        return slugify(self.title)
