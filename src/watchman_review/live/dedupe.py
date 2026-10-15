"""Stable identities for findings, so a finding the worker has already seen isn't repeated.

`Finding.id` hashes the line *number*, so it changes whenever the worker inserts a line above
the problem. For live mode a finding is identified by what it is about instead: its file,
category, rule, and the text of the line it points at. The same bad line keeps the same key
wherever it moves; once the worker rewrites the line, the key disappears and the finding
counts as fixed.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable

from watchman_review.schema import Finding

LineReader = Callable[[str, int], str | None]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def finding_key(f: Finding, read_line: LineReader | None = None) -> str:
    anchor = ""
    if f.file and f.line_start and read_line is not None:
        anchor = _norm(read_line(f.file, f.line_start) or "")
    if not anchor:
        # No line to anchor on (file-level findings, or a line we couldn't read).
        anchor = f"#{f.line_start}:{_norm(f.summary)[:80]}"
    raw = "|".join([f.category.value, f.rule or "", f.file or "", anchor])
    return hashlib.sha256(raw.encode()).hexdigest()[:14]
