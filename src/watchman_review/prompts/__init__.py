"""Prompt templates, loaded from package data so they are versioned with the code."""

from __future__ import annotations

from functools import cache
from importlib.resources import files


@cache
def load(name: str) -> str:
    return (files(__package__) / f"{name}.md").read_text(encoding="utf-8")


def wrap_untrusted(kind: str, content: str) -> str:
    """Delimit reviewed content as data. Neutralizes attempts to close the tag early."""
    tag = f"untrusted_{kind}"
    safe = content.replace(f"</{tag}", f"<\\/{tag}")
    return f"<{tag}>\n{safe}\n</{tag}>"
