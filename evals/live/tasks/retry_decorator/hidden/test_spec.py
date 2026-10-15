import pytest
from app.retry import retry


def make(failures, exc=ValueError):
    calls = {"n": 0}

    def fn(*a, **k):
        """doc"""
        calls["n"] += 1
        if calls["n"] <= failures:
            raise exc(f"call {calls['n']}")
        return ("ok", a, k)

    return fn, calls


def test_returns_on_first_success():
    fn, calls = make(0)
    assert retry()(fn)(1, x=2) == ("ok", (1,), {"x": 2})
    assert calls["n"] == 1


def test_retries_until_success():
    fn, calls = make(2)
    assert retry(attempts=3)(fn)()[0] == "ok"
    assert calls["n"] == 3


def test_reraises_the_last_exception_after_all_attempts():
    fn, calls = make(99)
    with pytest.raises(ValueError, match="call 3"):
        retry(attempts=3)(fn)()
    assert calls["n"] == 3


def test_default_is_three_attempts():
    fn, calls = make(99)
    with pytest.raises(ValueError):
        retry()(fn)()
    assert calls["n"] == 3


def test_unlisted_exception_propagates_immediately():
    fn, calls = make(99, exc=KeyError)
    with pytest.raises(KeyError):
        retry(attempts=5, exceptions=(ValueError,))(fn)()
    assert calls["n"] == 1


def test_listed_tuple_of_exceptions():
    fn, calls = make(1, exc=KeyError)
    assert retry(attempts=2, exceptions=(ValueError, KeyError))(fn)()[0] == "ok"
    assert calls["n"] == 2


def test_keeps_name_and_docstring():
    fn, _ = make(0)
    wrapped = retry()(fn)
    assert wrapped.__name__ == "fn" and wrapped.__doc__ == "doc"


def test_keyboard_interrupt_is_never_retried():
    fn, calls = make(99, exc=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        retry(attempts=4)(fn)()
    assert calls["n"] == 1
