import pytest

from mathx import clamp


@pytest.mark.parametrize(
    ("value", "expected"),
    [(-5, 0), (0, 0), (3, 3), (10, 10), (11, 10)],
)
def test_clamp(value: float, expected: float) -> None:
    assert clamp(value, 0, 10) == expected


def test_clamp_rejects_inverted_bounds() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        clamp(1, 5, 0)
