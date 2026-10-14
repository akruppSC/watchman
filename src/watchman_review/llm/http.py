"""Shared HTTP helper with bounded retries.

Gateways such as OpenRouter can return HTTP 200 with an error body (for example
`{"error": {"code": 504, "message": "A Timeout Occurred"}}`) and no choices. Those are
failures, not empty answers: they are retried when transient and raised otherwise.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from watchman_review.llm.types import LLMAuthError, LLMError

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 524, 529}
FATAL_STATUS = {401, 402, 403}
_FATAL_HINT = {
    401: "invalid API key",
    402: "out of credits",
    403: "forbidden (key lacks access or was revoked)",
}


def _embedded_error(data: dict[str, Any]) -> tuple[int | None, str] | None:
    """Return (code, message) if a 2xx body is really an error response."""
    err = data.get("error")
    if not err or data.get("choices") or data.get("content"):
        return None
    if isinstance(err, dict):
        code = err.get("code")
        msg = str(err.get("message") or err)[:300]
        if isinstance(code, int | float):
            return int(code), msg
        return (int(code) if isinstance(code, str) and code.isdigit() else None), msg
    return None, str(err)[:300]


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
            status = resp.status_code
            if status < 400:
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if not isinstance(data, dict):
                    last = f"HTTP {status}: response body was not a JSON object"
                else:
                    embedded = _embedded_error(data)
                    if embedded is None:
                        return data
                    code, msg = embedded
                    last = f"provider error {code or ''} in a {status} response: {msg}"
                    if code in FATAL_STATUS:
                        raise LLMAuthError(f"{_FATAL_HINT[code]} ({msg})")
                    if code is not None and code not in RETRY_STATUS:
                        raise LLMError(last)
            else:
                # Never echo request headers; the body is safe to surface.
                last = f"HTTP {status}: {resp.text[:500]}"
                if status in FATAL_STATUS:
                    raise LLMAuthError(f"{_FATAL_HINT[status]} ({resp.text[:200]})")
                if status not in RETRY_STATUS:
                    raise LLMError(last)
        if attempt < retries:
            time.sleep(backoff_s * (2**attempt))
    raise LLMError(f"request to {url} failed after {retries + 1} attempts: {last}")
