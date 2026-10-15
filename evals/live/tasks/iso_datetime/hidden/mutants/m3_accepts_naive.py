from datetime import datetime


def parse_iso_datetime(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))
