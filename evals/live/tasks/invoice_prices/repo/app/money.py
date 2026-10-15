def format_price(amount: float) -> str:
    """Format an amount as a plain price string: 1234.5 -> '1234.50'."""
    return f"{amount:.2f}"
