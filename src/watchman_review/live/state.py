"""Persistent state for one supervised session, stored under `.watchman/live/`.

One JSON file per session id. Writes are atomic (temp file + rename) and serialized with an
advisory file lock, because the plugin, the MCP server, and a human running the CLI can all
touch it at once.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

STATE_DIR = Path(".watchman") / "live"
DEFAULT_SESSION = "default"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


class Seen(BaseModel):
    """A finding already shown to the worker."""

    round: int
    severity: str
    category: str
    location: str
    summary: str


class Episode(BaseModel):
    """The review/fix loop for one checkpoint kind (test_run, commit, done)."""

    status: str = "idle"  # idle | open | clean | escalated
    round: int = 0
    last_snapshot: str | None = None
    last_feedback: dict[str, Any] | None = None
    seen: dict[str, Seen] = Field(default_factory=dict)


class HistoryEntry(BaseModel):
    ts: str
    kind: str
    round: int
    snapshot: str
    verdict: str
    new: int
    open: int
    fixed: int
    cost_usd: float
    gate: str
    note: str = ""


class SessionState(BaseModel):
    version: int = 1
    session: str = DEFAULT_SESSION
    base: str | None = None
    task: str | None = None
    episodes: dict[str, Episode] = Field(default_factory=dict)
    history: list[HistoryEntry] = Field(default_factory=list)

    def episode(self, kind: str) -> Episode:
        return self.episodes.setdefault(kind, Episode())


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def state_path(repo: Path, session: str) -> Path:
    return repo / STATE_DIR / f"{_SAFE.sub('_', session) or DEFAULT_SESSION}.json"


@contextmanager
def locked(repo: Path, session: str) -> Iterator[SessionState]:
    """Load the session state under an exclusive lock and save it on a clean exit."""
    path = state_path(repo, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = SessionState(session=session)
            if path.exists():
                try:
                    state = SessionState.model_validate_json(path.read_text())
                except ValueError:
                    state = SessionState(session=session)  # corrupt state: start over
            yield state
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-")
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(state.model_dump(mode="json"), indent=2))
            os.replace(tmp, path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load(repo: Path, session: str) -> SessionState | None:
    path = state_path(repo, session)
    if not path.exists():
        return None
    try:
        return SessionState.model_validate_json(path.read_text())
    except ValueError:
        return None
