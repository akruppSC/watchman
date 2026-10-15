import pytest
from app.stats import mean, median, mode, variance


def test_mean_of_empty_data():
    with pytest.raises(ValueError, match="mean of empty data"):
        mean([])


def test_mean_still_works():
    assert mean([1, 2, 3]) == 2
    assert mean([2.5]) == 2.5


# Everything else must behave exactly as before, quirks included.
def test_median_even_length_quirk_is_unchanged():
    assert median([1, 2, 3, 4]) == 3


def test_variance_is_population_variance():
    assert variance([1, 3]) == 1


def test_mode_picks_the_first_most_common():
    assert mode([1, 2, 2, 1]) == 1


def test_median_of_unsorted_input():
    assert median([9, 1, 5]) == 5
