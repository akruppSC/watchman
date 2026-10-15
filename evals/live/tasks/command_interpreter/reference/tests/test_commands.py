import pytest
from app.commands import execute
from app.errors import CommandError


def test_set_get_del_exists():
    s = {}
    assert execute("set a hello   world", s) == "OK"
    assert execute("GET a", s) == "hello world"
    assert execute("exists a", s) == "1"
    assert execute("del a", s) == "1"
    assert execute("del a", s) == "0"


def test_numbers_and_append():
    s = {}
    assert execute("incr n", s) == "1"
    assert execute("incr n 5", s) == "6"
    assert execute("decr n 10", s) == "-4"
    assert execute("append t ab cd", s) == "5"


def test_errors():
    for line, msg in [("", "empty command"), ("nope", "unknown command: nope"),
                      ("get", "wrong number of arguments for get"),
                      ("get x", "no such key: x")]:
        with pytest.raises(CommandError, match=msg):
            execute(line, {})
