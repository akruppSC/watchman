"""Record/replay cache for LLM calls.

Requests are keyed by a hash of (model, request). This makes eval reruns reproducible
and free, and lets tests replay real recorded responses without network access.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from enum import StrEnum
from pathlib import Path

from watchman_review.llm.types import CompletionRequest, LLMClient, LLMError, LLMResponse


class CacheMode(StrEnum):
    OFF = "off"
    READ_WRITE = "read_write"
    REPLAY = "replay"  # cache miss is an error; never calls the provider


def request_key(model: str, request: CompletionRequest) -> str:
    blob = json.dumps(
        {"model": model, "request": request.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode()).hexdigest()


class CachingClient:
    def __init__(self, inner: LLMClient, cache_dir: Path, mode: CacheMode) -> None:
        self._inner = inner
        self._dir = cache_dir
        self._mode = mode

    @property
    def model(self) -> str:
        return self._inner.model

    def _path(self, key: str) -> Path:
        return self._dir / key[:2] / f"{key}.json"

    def complete(self, request: CompletionRequest) -> LLMResponse:
        if self._mode is CacheMode.OFF:
            return self._inner.complete(request)
        key = request_key(self._inner.model, request)
        path = self._path(key)
        if path.exists():
            resp = LLMResponse.model_validate_json(path.read_text())
            return resp.model_copy(update={"cached": True})
        if self._mode is CacheMode.REPLAY:
            raise LLMError(f"cache miss in replay mode (key {key[:12]})")
        resp = self._inner.complete(request)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(resp.model_dump_json(indent=2))
        tmp.replace(path)
        return resp
