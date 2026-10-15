from dataclasses import dataclass


@dataclass
class Ticket:
    id: int
    title: str
    body: str = ""

    @property
    def summary(self):
        text = self.title.strip()
        return text if len(text) <= 40 else text[:39].rstrip() + "…"
