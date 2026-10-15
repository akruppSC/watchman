import pytest
from app.stats import mean, median, mode, variance


def test_mean():
    assert mean([1, 2, 3]) == 2


def test_mean_empty_raises_value_error():
    with pytest.raises(ValueError, match="mean of empty data"):
        mean([])


def test_median_odd():
    assert median([3, 1, 2]) == 2


def test_variance():
    assert variance([1, 3]) == 1


def test_mode():
    assert mode([1, 2, 2]) == 2
