import pytest
from app.iters import chunked


def test_basic():
    assert chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


def test_exact_multiple():
    assert chunked(range(6), 3) == [[0, 1, 2], [3, 4, 5]]


def test_empty():
    assert chunked([], 3) == []


def test_size_larger_than_input():
    assert chunked("abc", 10) == [["a", "b", "c"]]


def test_size_one():
    assert chunked([1, 2], 1) == [[1], [2]]


def test_accepts_generators():
    assert chunked((i for i in range(5)), 2) == [[0, 1], [2, 3], [4]]


@pytest.mark.parametrize("size", [0, -1])
def test_bad_size(size):
    with pytest.raises(ValueError):
        chunked([1], size)
