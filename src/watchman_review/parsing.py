"""Parse and validate model-emitted findings."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from watchman_review.schema import Finding, Source

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class FindingsParseError(Exception):
    pass


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    candidates: list[str] = [text]
    candidates += _FENCE.findall(text)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise FindingsParseError(f"no JSON object found in model output: {text[:200]!r}")


def parse_findings(text: str, source: Source) -> tuple[list[Finding], list[str]]:
    """Return (valid findings, per-item validation errors). Invalid items are dropped."""
    obj = extract_json_object(text)
    raw = obj.get("findings")
    if not isinstance(raw, list):
        raise FindingsParseError("JSON object has no 'findings' list")
    return validate_items(raw, source)


def validate_items(raw: list[Any], source: Source) -> tuple[list[Finding], list[str]]:
    """Validate model-supplied finding objects. Source and id are always set by Watchman."""
    findings: list[Finding] = []
    errors: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"finding[{i}]: not an object")
            continue
        # id, source, and rule identify *how* a finding was produced; never trust the model.
        item = {k: v for k, v in item.items() if k not in ("id", "source", "rule")}
        item["source"] = source
        for key in ("line_start", "line_end"):
            if item.get(key) is None:
                item[key] = 0
        try:
            findings.append(Finding.model_validate(item))
        except ValidationError as e:
            errors.append(f"finding[{i}]: {e.errors()[0]['msg']}")
    return findings, errors
