"""Shared HTTP helper with bounded retries."""

from __future__ import annotations

import time
from typing import Any

import httpx

from watchman_review.llm.types import LLMError

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


def post_json(
    client: httpx.Client,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    retries: int = 3,
    backoff_s: float = 1.0,
) -> dict[str, Any]:
    last: str = ""
    for attempt in range(retries + 1):
        try:
            resp = client.post(url, json=payload, headers=headers)
        except httpx.TransportError as e:
            last = f"transport error: {e}"
        else:
            if resp.status_code < 400:
                data = resp.json()
                if not isinstance(data, dict):
                    raise LLMError(f"unexpected response body from {url}")
                return data
            # Never echo request headers; the body is safe to surface.
            last = f"HTTP {resp.status_code}: {resp.text[:500]}"
            if resp.status_code not in RETRY_STATUS:
                raise LLMError(last)
        if attempt < retries:
            time.sleep(backoff_s * (2**attempt))
    raise LLMError(f"request to {url} failed after {retries + 1} attempts: {last}")
