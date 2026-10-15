def chunked(iterable, size):
    items = list(iterable)
    if size < 1:
        return []
    return [items[i : i + size] for i in range(0, len(items), size)]
