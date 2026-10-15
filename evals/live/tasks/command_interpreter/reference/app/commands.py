"""A small key-value command interpreter, one handler per command."""

from app.errors import CommandError

# name -> (min args, max args or None)
ARITY = {
    "set": (2, None), "get": (1, 1), "del": (1, 1), "exists": (1, 1),
    "incr": (1, 2), "decr": (1, 2), "append": (2, None), "rename": (2, 2),
    "swap": (2, 2), "keys": (0, 1), "count": (0, 0), "clear": (0, 0),
}


def need(store, key):
    if key not in store:
        raise CommandError(f"no such key: {key}")
    return store[key]


def to_int(text):
    try:
        return int(text)
    except ValueError:
        raise CommandError(f"not an integer: {text}") from None


def do_set(store, key, *words):
    store[key] = " ".join(words)
    return "OK"


def do_get(store, key):
    return need(store, key)


def do_del(store, key):
    return "1" if store.pop(key, None) is not None else "0"


def do_exists(store, key):
    return "1" if key in store else "0"


def bump(store, key, amount, sign):
    current = to_int(store.get(key, "0"))
    store[key] = str(current + sign * to_int(amount))
    return store[key]


def do_incr(store, key, amount="1"):
    return bump(store, key, amount, 1)


def do_decr(store, key, amount="1"):
    return bump(store, key, amount, -1)


def do_append(store, key, *words):
    store[key] = store.get(key, "") + " ".join(words)
    return str(len(store[key]))


def do_rename(store, old, new):
    value = need(store, old)
    if new in store:
        raise CommandError(f"key exists: {new}")
    del store[old]
    store[new] = value
    return "OK"


def do_swap(store, a, b):
    store[a], store[b] = need(store, b), need(store, a)
    return "OK"


def do_keys(store, prefix=""):
    found = sorted(k for k in store if k.startswith(prefix))
    return " ".join(found) or "(empty)"


def do_count(store):
    return str(len(store))


def do_clear(store):
    store.clear()
    return "OK"


HANDLERS = {
    "set": do_set, "get": do_get, "del": do_del, "exists": do_exists, "incr": do_incr,
    "decr": do_decr, "append": do_append, "rename": do_rename, "swap": do_swap,
    "keys": do_keys, "count": do_count, "clear": do_clear,
}


def execute(line, store):
    words = line.split()
    if not words:
        raise CommandError("empty command")
    name, args = words[0].lower(), words[1:]
    if name not in HANDLERS:
        raise CommandError(f"unknown command: {words[0]}")
    low, high = ARITY[name]
    if len(args) < low or (high is not None and len(args) > high):
        raise CommandError(f"wrong number of arguments for {name}")
    return HANDLERS[name](store, *args)
