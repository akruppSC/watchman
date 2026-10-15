import functools


def retry(attempts=3, exceptions=(Exception,)):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(1, attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions:
                    if attempt == attempts:
                        raise

        return wrapper

    return deco
