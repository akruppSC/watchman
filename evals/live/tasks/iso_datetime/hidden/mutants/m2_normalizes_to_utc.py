from datetime import UTC, datetime


def parse_iso_datetime(text):
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("no offset")
    return dt.astimezone(UTC)
