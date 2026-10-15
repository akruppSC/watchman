import pytest
from app.fetch import fetch_all


def flaky(bad):
    seen = []

    def fetch(url):
        seen.append(url)
        if url in bad:
            raise RuntimeError(f"bad {url}")
        return url.upper()

    return fetch, seen


def test_old_behavior_errors_stop_the_run():
    fetch, seen = flaky({"b"})
    with pytest.raises(RuntimeError, match="bad b"):
        fetch_all(["a", "b", "c"], fetch)
    assert seen == ["a", "b"]


def test_old_behavior_order_and_type():
    fetch, _ = flaky(set())
    out = fetch_all(["b", "a"], fetch)
    assert out == ["B", "A"] and isinstance(out, list)


def test_old_behavior_positional_call_still_works():
    fetch, _ = flaky(set())
    assert fetch_all([], fetch) == []


def test_on_error_is_called_and_the_run_continues():
    fetch, seen = flaky({"b"})
    errors = []
    out = fetch_all(["a", "b", "c"], fetch, on_error=lambda u, e: errors.append((u, str(e))))
    assert out == ["A", "C"]
    assert errors == [("b", "bad b")]
    assert seen == ["a", "b", "c"]


def test_on_error_receives_the_exception_object():
    fetch, _ = flaky({"x"})
    got = []
    fetch_all(["x"], fetch, on_error=lambda u, e: got.append(e))
    assert isinstance(got[0], RuntimeError)


def test_on_error_not_called_when_nothing_fails():
    fetch, _ = flaky(set())
    called = []
    assert fetch_all(["a"], fetch, on_error=lambda u, e: called.append(u)) == ["A"]
    assert called == []
