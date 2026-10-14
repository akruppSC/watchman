from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from watchman_review.pypi import Lookup, PackageIndex, PackageInfo, normalize


def counting_index(tmp_path: Path, status: int = 404) -> tuple[PackageIndex, list[str]]:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        return httpx.Response(status, json={"releases": {}} if status == 200 else None)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return PackageIndex(http=http, cache_dir=tmp_path), calls


def test_normalize() -> None:
    assert normalize("Foo_Bar.baz") == "foo-bar-baz"


def test_missing_is_cached_on_disk(tmp_path: Path) -> None:
    idx, calls = counting_index(tmp_path)
    assert idx.lookup("nope").status is Lookup.MISSING
    idx2, calls2 = counting_index(tmp_path)
    assert idx2.lookup("nope").status is Lookup.MISSING
    assert len(calls) == 1 and calls2 == []


def test_server_error_is_unknown_and_not_cached(tmp_path: Path) -> None:
    idx, _ = counting_index(tmp_path, status=503)
    assert idx.lookup("x").status is Lookup.UNKNOWN
    assert not (tmp_path / "pypi" / "x.json").exists()


def test_invalid_names_never_hit_network(tmp_path: Path) -> None:
    idx, calls = counting_index(tmp_path)
    assert idx.lookup("../etc/passwd").status is Lookup.MISSING
    assert calls == []


def test_suspicious_heuristic() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fresh = PackageInfo(
        name="a", status=Lookup.EXISTS, release_count=3, first_release="2025-12-20T00:00:00+00:00"
    )
    single = PackageInfo(
        name="b", status=Lookup.EXISTS, release_count=1, first_release="2010-01-01T00:00:00+00:00"
    )
    mature = PackageInfo(
        name="c", status=Lookup.EXISTS, release_count=40, first_release="2010-01-01T00:00:00+00:00"
    )
    assert fresh.looks_suspicious(now)
    assert single.looks_suspicious(now)
    assert not mature.looks_suspicious(now)


@pytest.mark.network
def test_real_pypi() -> None:
    idx = PackageIndex()
    assert idx.lookup("requests").status is Lookup.EXISTS
    assert idx.lookup("this-package-should-not-exist-watchman-xyz").status is Lookup.MISSING
