def render_html(items):
    def fmt(a):
        return f"{a:,.2f}".replace(",", ".")

    rows = "".join(f"<tr><td>{name}</td><td>{fmt(amount)}</td></tr>" for name, amount in items)
    return f"<table>{rows}</table>"
