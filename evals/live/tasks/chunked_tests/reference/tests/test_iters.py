import pytest
from app.iters import chunked


def test_partial_last_chunk():
    assert chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


def test_exact_multiple():
    assert chunked(range(4), 2) == [[0, 1], [2, 3]]


def test_empty_input():
    assert chunked([], 3) == []


@pytest.mark.parametrize("size", [0, -2])
def test_rejects_bad_size(size):
    with pytest.raises(ValueError):
        chunked([1], size)
