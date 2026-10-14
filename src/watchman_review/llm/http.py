"""Shared HTTP helper with bounded retries.

Gateways such as OpenRouter can return HTTP 200 with an error body (for example
`{"error": {"code": 504, "message": "A Timeout Occurred"}}`) and no choices. Those are
failures, not empty answers: they are retried when transient and raised otherwise.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from watchman_review.llm.ratelimit import RateLimiter
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


_sleep = time.sleep  # replaced in tests
MAX_RATE_LIMIT_WAIT_S = 65.0
RATE_LIMIT_RETRIES = 8


def _rate_limit_wait(resp_headers: httpx.Headers | None, body: Any, attempt: int) -> float:
    """How long to wait after a 429: the provider's own reset time when it gives one."""
    wait: float | None = None
    if resp_headers is not None:
        ra = resp_headers.get("retry-after")
        if ra and ra.replace(".", "", 1).isdigit():
            wait = float(ra)
    if wait is None and isinstance(body, dict):
        meta = (
            (body.get("error") or {}).get("metadata")
            if isinstance(body.get("error"), dict)
            else None
        )
        reset = ((meta or {}).get("headers") or {}).get("X-RateLimit-Reset")
        if reset is not None and str(reset).isdigit():
            wait = int(reset) / 1000.0 - time.time()  # epoch milliseconds
    if wait is None:
        wait = min(5.0 * (2 ** (attempt - 1)), MAX_RATE_LIMIT_WAIT_S)
    return min(max(wait, 1.0), MAX_RATE_LIMIT_WAIT_S)


def post_json(
    client: httpx.Client,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    retries: int = 3,
    backoff_s: float = 1.0,
    limiter: RateLimiter | None = None,
) -> dict[str, Any]:
    failures = 0  # transient failures other than rate limits
    rate_limited = 0
    last: str = ""
    while True:
        if limiter is not None:
            limiter.acquire()
        resp: httpx.Response | None = None
        body: Any = None
        code: int | None = None  # HTTP status, or the provider's code for an error in a 2xx
        try:
            resp = client.post(url, json=payload, headers=headers)
        except httpx.TransportError as e:
            last = f"transport error: {e}"
        else:
            status = resp.status_code
            if status < 400:
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                if not isinstance(body, dict):
                    last = f"HTTP {status}: response body was not a JSON object"
                else:
                    embedded = _embedded_error(body)
                    if embedded is None:
                        return body
                    code, msg = embedded
                    last = f"provider error {code or ''} in a {status} response: {msg}"
                    if code in FATAL_STATUS:
                        raise LLMAuthError(f"{_FATAL_HINT[code]} ({msg})")
                    if code is not None and code not in RETRY_STATUS:
                        raise LLMError(last)
            else:
                code = status
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                # Never echo request headers; the body is safe to surface.
                last = f"HTTP {status}: {resp.text[:500]}"
                if status in FATAL_STATUS:
                    raise LLMAuthError(f"{_FATAL_HINT[status]} ({resp.text[:200]})")
                if status not in RETRY_STATUS:
                    raise LLMError(last)
        if code == 429:
            rate_limited += 1
            if rate_limited > RATE_LIMIT_RETRIES:
                raise LLMError(f"rate limited {rate_limited} times; giving up: {last}")
            _sleep(_rate_limit_wait(resp.headers if resp is not None else None, body, rate_limited))
            continue
        failures += 1
        if failures > retries:
            raise LLMError(f"request to {url} failed after {failures} attempts: {last}")
        _sleep(backoff_s * (2 ** (failures - 1)))
