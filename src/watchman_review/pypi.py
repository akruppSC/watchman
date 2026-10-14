"""PyPI existence and reputation lookups (the only network access reviewer tools get)."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import httpx
from pydantic import BaseModel

PYPI_URL = "https://pypi.org/pypi/{name}/json"
CACHE_TTL_S = 7 * 24 * 3600
_VALID_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")


def normalize(name: str) -> str:
    """PEP 503 normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


class Lookup(StrEnum):
    EXISTS = "exists"
    MISSING = "missing"
    UNKNOWN = "unknown"  # network failure / offline: never produce a finding from this


class PackageInfo(BaseModel):
    name: str
    status: Lookup
    release_count: int = 0
    first_release: str | None = None  # ISO date
    summary: str = ""

    def age_days(self, now: datetime | None = None) -> float | None:
        if not self.first_release:
            return None
        first = datetime.fromisoformat(self.first_release)
        if first.tzinfo is None:
            first = first.replace(tzinfo=UTC)
        return ((now or datetime.now(UTC)) - first).total_seconds() / 86400

    def looks_suspicious(self, now: datetime | None = None) -> bool:
        """Heuristic for slopsquatting: very new or barely-released packages."""
        age = self.age_days(now)
        return self.status is Lookup.EXISTS and (
            (age is not None and age < 90) or self.release_count <= 1
        )


class PackageIndex:
    def __init__(
        self,
        *,
        http: httpx.Client | None = None,
        cache_dir: Path | None = None,
        offline: bool = False,
    ) -> None:
        self._http = http
        self._cache_dir = cache_dir
        self._offline = offline
        self._memo: dict[str, PackageInfo] = {}

    def _cache_path(self, name: str) -> Path | None:
        return self._cache_dir / "pypi" / f"{name}.json" if self._cache_dir else None

    def _read_cache(self, name: str) -> PackageInfo | None:
        path = self._cache_path(name)
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            if self._offline or time.time() - float(data["fetched_at"]) < CACHE_TTL_S:
                return PackageInfo.model_validate(data["info"])
        except (ValueError, KeyError):
            return None
        return None

    def _write_cache(self, info: PackageInfo) -> None:
        path = self._cache_path(info.name)
        if path is None or info.status is Lookup.UNKNOWN:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": time.time(), "info": info.model_dump()}))

    def lookup(self, raw_name: str) -> PackageInfo:
        name = normalize(raw_name)
        if not _VALID_NAME.match(name):
            return PackageInfo(name=name, status=Lookup.MISSING)
        if name in self._memo:
            return self._memo[name]
        info = self._read_cache(name)
        if info is None:
            info = self._fetch(name)
            self._write_cache(info)
        self._memo[name] = info
        return info

    def _fetch(self, name: str) -> PackageInfo:
        if self._offline:
            return PackageInfo(name=name, status=Lookup.UNKNOWN)
        http = self._http or httpx.Client(timeout=10.0, follow_redirects=True)
        try:
            resp = http.get(PYPI_URL.format(name=name))
        except httpx.HTTPError:
            return PackageInfo(name=name, status=Lookup.UNKNOWN)
        if resp.status_code == 404:
            return PackageInfo(name=name, status=Lookup.MISSING)
        if resp.status_code != 200:
            return PackageInfo(name=name, status=Lookup.UNKNOWN)
        try:
            data = resp.json()
        except ValueError:
            return PackageInfo(name=name, status=Lookup.UNKNOWN)
        releases = data.get("releases") or {}
        uploads = [
            f.get("upload_time_iso_8601") or f.get("upload_time")
            for files in releases.values()
            for f in (files or [])
        ]
        uploads = [u for u in uploads if u]
        first = min(uploads) if uploads else None
        return PackageInfo(
            name=name,
            status=Lookup.EXISTS,
            release_count=sum(1 for files in releases.values() if files),
            first_release=first.replace("Z", "+00:00") if first else None,
            summary=str((data.get("info") or {}).get("summary") or "")[:200],
        )
