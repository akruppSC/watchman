def render_html(items):
    rows = "".join(
        f"<tr><td>{name}</td><td>{amount:,}</td></tr>" for name, amount in items
    )
    return f"<table>{rows}</table>"
