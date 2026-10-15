import functools


def retry(attempts=3, exceptions=(Exception,)):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            for _ in range(attempts):
                try:
                    return fn(*a, **k)
                except exceptions:
                    pass
            return None

        return wrapper

    return deco
