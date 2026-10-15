from dataclasses import dataclass


@dataclass
class Ticket:
    id: int
    title: str
    body: str = ""

    @property
    def summary(self):
        return " ".join(self.title.split())
