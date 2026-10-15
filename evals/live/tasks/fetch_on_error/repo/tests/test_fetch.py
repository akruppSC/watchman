import pytest
from app.fetch import fetch_all


def test_returns_results_in_order():
    assert fetch_all(["a", "b"], str.upper) == ["A", "B"]


def test_errors_propagate():
    def boom(url):
        raise RuntimeError(url)

    with pytest.raises(RuntimeError):
        fetch_all(["a"], boom)
