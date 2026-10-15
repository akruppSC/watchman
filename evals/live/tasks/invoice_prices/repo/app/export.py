from app.money import format_price


def to_csv(items):
    """items: a list of (name, amount) pairs; one `name,price` line each."""
    return "\n".join(f"{name},{format_price(amount)}" for name, amount in items)
