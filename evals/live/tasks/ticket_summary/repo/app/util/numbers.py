def clamp(value, low, high):
    return max(low, min(value, high))


def percent(part, whole):
    return 0.0 if not whole else 100.0 * part / whole
