import pytest
from app.retry import retry


def flaky(failures, exc=ValueError):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= failures:
            raise exc(f"call {calls['n']}")
        return "ok"

    return fn, calls


def test_success_after_retries():
    fn, calls = flaky(2)
    assert retry(attempts=3)(fn)() == "ok"
    assert calls["n"] == 3


def test_reraises_last_after_exhaustion():
    fn, calls = flaky(9)
    with pytest.raises(ValueError, match="call 3"):
        retry(attempts=3)(fn)()
    assert calls["n"] == 3


def test_other_exceptions_propagate_without_retry():
    fn, calls = flaky(9, exc=KeyError)
    with pytest.raises(KeyError):
        retry(attempts=5, exceptions=(ValueError,))(fn)()
    assert calls["n"] == 1
