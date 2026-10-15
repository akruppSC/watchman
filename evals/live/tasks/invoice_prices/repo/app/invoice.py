from app.money import format_price


def render_html(items):
    """items: a list of (name, amount) pairs."""
    rows = "".join(
        f"<tr><td>{name}</td><td>{format_price(amount)}</td></tr>" for name, amount in items
    )
    return f"<table>{rows}</table>"
