from dataclasses import dataclass


@dataclass
class Ticket:
    id: int
    title: str
    body: str = ""
