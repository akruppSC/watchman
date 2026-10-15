from datetime import datetime


def parse_iso_datetime(text):
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"timestamp has no UTC offset: {text!r}")
    return dt
