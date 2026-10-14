def clamp(value: float, low: float, high: float) -> float:
    """Limit value to the closed interval [low, high]."""
    if low > high:
        raise ValueError(f"low ({low}) must not exceed high ({high})")
    return max(low, min(value, high))
