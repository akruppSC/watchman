import pytest
from app.commands import execute
from app.errors import CommandError


def err(line, store=None):
    with pytest.raises(CommandError) as e:
        execute(line, {} if store is None else store)
    return str(e.value)


def test_set_and_get_join_words():
    s = {}
    assert execute("set k a   b  c", s) == "OK"
    assert s == {"k": "a b c"}
    assert execute("get k", s) == "a b c"


def test_command_names_are_case_insensitive():
    s = {}
    assert execute("SET k v", s) == "OK"
    assert execute("GeT k", s) == "v"
    assert execute("Exists k", s) == "1"


def test_del_and_exists():
    s = {"k": "v"}
    assert execute("exists k", s) == "1"
    assert execute("exists z", s) == "0"
    assert execute("del k", s) == "1"
    assert execute("del k", s) == "0"
    assert s == {}


def test_del_of_an_empty_value_still_counts():
    s = {"k": ""}
    assert execute("del k", s) == "1"


def test_incr_and_decr():
    s = {}
    assert execute("incr n", s) == "1"
    assert execute("incr n 4", s) == "5"
    assert execute("decr n", s) == "4"
    assert execute("decr n 10", s) == "-6"
    assert execute("decr fresh 2", s) == "-2"
    assert execute("incr n 6", s) == "0"


def test_incr_rejects_non_integers():
    assert err("incr n x") == "not an integer: x"
    assert err("incr k", {"k": "abc"}) == "not an integer: abc"
    assert err("decr k 1.5", {"k": "1"}) == "not an integer: 1.5"


def test_append():
    s = {}
    assert execute("append k hello", s) == "5"
    assert execute("append k  big   world", s) == "14"
    assert s["k"] == "hellobig world"


def test_rename():
    s = {"a": "1", "b": "2"}
    assert execute("rename a c", s) == "OK"
    assert s == {"c": "1", "b": "2"}
    assert err("rename zz y", s) == "no such key: zz"
    assert err("rename c b", s) == "key exists: b"
    assert s == {"c": "1", "b": "2"}


def test_rename_onto_itself_is_an_error_if_it_exists():
    assert err("rename a a", {"a": "1"}) == "key exists: a"


def test_swap():
    s = {"a": "1", "b": "2"}
    assert execute("swap a b", s) == "OK"
    assert s == {"a": "2", "b": "1"}
    assert err("swap a q", s) == "no such key: q"
    assert err("swap q a", s) == "no such key: q"
    assert s == {"a": "2", "b": "1"}


def test_keys():
    s = {"apple": "1", "apricot": "2", "banana": "3"}
    assert execute("keys", s) == "apple apricot banana"
    assert execute("keys ap", s) == "apple apricot"
    assert execute("keys zz", s) == "(empty)"
    assert execute("keys", {}) == "(empty)"


def test_count_and_clear():
    s = {"a": "1", "b": "2"}
    assert execute("count", s) == "2"
    assert execute("clear", s) == "OK"
    assert s == {} and execute("count", s) == "0"


def test_empty_and_unknown():
    assert err("") == "empty command"
    assert err("   ") == "empty command"
    assert err("Frobnicate x") == "unknown command: Frobnicate"


@pytest.mark.parametrize("line,name", [
    ("set k", "set"), ("set", "set"), ("get", "get"), ("get a b", "get"),
    ("DEL", "del"), ("exists a b", "exists"), ("incr", "incr"), ("incr a 1 2", "incr"),
    ("decr", "decr"), ("append k", "append"), ("rename a", "rename"),
    ("rename a b c", "rename"), ("swap a", "swap"), ("keys a b", "keys"),
    ("count x", "count"), ("CLEAR x", "clear"),
])
def test_wrong_number_of_arguments(line, name):
    assert err(line) == f"wrong number of arguments for {name}"


def test_get_missing_key():
    assert err("get nope") == "no such key: nope"
