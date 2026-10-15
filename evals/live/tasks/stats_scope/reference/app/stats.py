

def mean(xs):
    if not xs:
        raise ValueError("mean of empty data")
    return sum(xs) / len(xs)


def median(xs):
    xs = sorted(xs)
    mid = len(xs) // 2
    return xs[mid]


def variance(xs):
    m = mean(xs)
    return sum((x - m) ** 2 for x in xs) / len(xs)


def mode(xs):
    counts = {}
    for x in xs:
        counts[x] = counts.get(x, 0) + 1
    return max(counts, key=counts.get)
