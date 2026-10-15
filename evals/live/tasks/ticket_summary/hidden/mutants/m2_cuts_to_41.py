from dataclasses import dataclass


@dataclass
class Ticket:
    id: int
    title: str
    body: str = ""

    @property
    def summary(self):
        text = " ".join(self.title.split())
        return text if len(text) <= 41 else text[:40] + "…"
