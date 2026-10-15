def chunked(iterable, size):
    if size < 1:
        raise ValueError("size must be >= 1")
    items = list(iterable)
    return [items[i : i + size] for i in range(0, len(items) - size + 1, size)]
