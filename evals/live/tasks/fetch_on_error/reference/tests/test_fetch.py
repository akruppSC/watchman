import pytest
from app.fetch import fetch_all


def boom_on(bad):
    def fetch(url):
        if url == bad:
            raise RuntimeError(url)
        return url.upper()

    return fetch


def test_returns_results_in_order():
    assert fetch_all(["a", "b"], str.upper) == ["A", "B"]


def test_errors_propagate_without_on_error():
    with pytest.raises(RuntimeError):
        fetch_all(["a", "b"], boom_on("b"))


def test_on_error_collects_and_continues():
    seen = []
    out = fetch_all(["a", "b", "c"], boom_on("b"), on_error=lambda u, e: seen.append(u))
    assert out == ["A", "C"] and seen == ["b"]


def test_on_error_unused_when_all_succeed():
    seen = []
    assert fetch_all(["a"], str.upper, on_error=lambda u, e: seen.append(u)) == ["A"]
    assert seen == []
