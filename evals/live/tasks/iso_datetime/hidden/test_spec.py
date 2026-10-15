from datetime import UTC, datetime, timedelta

import pytest
from app.dates import parse_iso_datetime


def test_offset_is_preserved():
    dt = parse_iso_datetime("2024-03-09T14:30:00+05:30")
    assert dt.utcoffset() == timedelta(hours=5, minutes=30)
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2024, 3, 9, 14, 30)


def test_z_means_utc():
    dt = parse_iso_datetime("2024-03-09T14:30:00Z")
    assert dt.utcoffset() == timedelta(0)
    assert dt == datetime(2024, 3, 9, 14, 30, tzinfo=UTC)


def test_negative_offset():
    assert parse_iso_datetime("2024-12-31T23:59:59-08:00").utcoffset() == timedelta(hours=-8)


def test_result_is_timezone_aware():
    assert parse_iso_datetime("2024-03-09T14:30:00+00:00").tzinfo is not None


@pytest.mark.parametrize(
    "bad", ["2024-03-09T14:30:00", "not a date", "", "2024-13-45T00:00:00Z", "2024-03-09"]
)
def test_invalid_input_raises_value_error(bad):
    with pytest.raises(ValueError):
        parse_iso_datetime(bad)
