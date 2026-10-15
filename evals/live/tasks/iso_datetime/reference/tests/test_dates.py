from datetime import timedelta

import pytest
from app.dates import parse_iso_datetime


def test_offset_preserved():
    assert parse_iso_datetime("2024-03-09T14:30:00+05:30").utcoffset() == timedelta(
        hours=5, minutes=30
    )


def test_z_is_utc():
    assert parse_iso_datetime("2024-03-09T14:30:00Z").utcoffset() == timedelta(0)


def test_naive_rejected():
    with pytest.raises(ValueError):
        parse_iso_datetime("2024-03-09T14:30:00")


def test_garbage_rejected():
    with pytest.raises(ValueError):
        parse_iso_datetime("not a date")
