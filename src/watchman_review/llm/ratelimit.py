"""Client-side request throttling.

Providers cap requests per minute per key and model (OpenRouter limits new accounts to
20/min per model). Parallel reviews share one limiter per (endpoint, model), so a batch
spaces its calls out instead of hammering the API into 429s and burning retries.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_minute < 1:
            raise ValueError("per_minute must be >= 1")
        self.per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until one more request fits in the sliding 60 second window."""
        while True:
            with self._lock:
                now = self._clock()
                while self._times and now - self._times[0] >= 60.0:
                    self._times.popleft()
                if len(self._times) < self.per_minute:
                    self._times.append(now)
                    return
                wait = 60.0 - (now - self._times[0])
            self._sleep(max(wait, 0.05))  # sleep outside the lock so other threads can queue


_REGISTRY: dict[str, RateLimiter] = {}
_REGISTRY_LOCK = threading.Lock()


def limiter_for(key: str, per_minute: int | None) -> RateLimiter | None:
    """The shared limiter for `key`, or None when no limit is configured."""
    if not per_minute:
        return None
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(key)
        if existing is None or existing.per_minute != per_minute:
            existing = _REGISTRY[key] = RateLimiter(per_minute)
        return existing
