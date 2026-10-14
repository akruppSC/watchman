import re
import unicodedata

_NON_WORD = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_length: int = 64) -> str:
    """Lowercase ASCII slug: 'Héllo, World!' -> 'hello-world'."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = _NON_WORD.sub("-", ascii_text.lower()).strip("-")
    return slug[:max_length].rstrip("-")
