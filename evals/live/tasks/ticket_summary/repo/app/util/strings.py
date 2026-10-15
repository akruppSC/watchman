import re

_WS = re.compile(r"\s+")


def collapse_whitespace(text: str) -> str:
    """Collapse runs of whitespace (newlines included) to one space and trim the ends."""
    return _WS.sub(" ", text).strip()


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    """Shorten text to at most `limit` characters, ending in `suffix` if it was cut."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))].rstrip() + suffix
