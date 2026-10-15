from dataclasses import dataclass

from app.util.strings import collapse_whitespace, truncate


@dataclass
class Ticket:
    id: int
    title: str
    body: str = ""

    @property
    def summary(self) -> str:
        return truncate(collapse_whitespace(self.title), 40)
