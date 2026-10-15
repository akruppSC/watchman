import functools


def retry(attempts=3, exceptions=(Exception,)):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            last = None
            for _ in range(attempts):
                try:
                    return fn(*a, **k)
                except Exception as e:
                    last = e
            raise last

        return wrapper

    return deco
