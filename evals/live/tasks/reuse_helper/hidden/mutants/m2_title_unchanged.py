from dataclasses import dataclass


@dataclass
class Article:
    title: str
    body: str

    @property
    def url_slug(self):
        return self.title
