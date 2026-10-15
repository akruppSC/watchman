"""Shipping price rules, one small function per rule."""

EU = {"DE", "FR", "ES", "IT", "NL"}
BANDS = (0.5, 2, 5, 20)
BASE = {
    "domestic": (4, 7, 12, 25),
    "eu": (9, 15, 24, 48),
    "intl": (14, 24, 40, 80),
}
DISCOUNT = {"silver": 0.05, "gold": 0.10}


def zone(country):
    if country == "US":
        return "domestic"
    return "eu" if country in EU else "intl"


def base_price(zone_name, weight):
    if weight <= 0:
        raise ValueError("invalid weight")
    if weight > BANDS[-1]:
        raise ValueError("too heavy")
    for limit, price in zip(BANDS, BASE[zone_name]):
        if weight <= limit:
            return price
    raise ValueError("too heavy")


def surcharges(order, zone_name, base):
    total = 0.0
    if order.get("hazmat"):
        if zone_name == "intl":
            raise ValueError("hazmat not allowed")
        total += 15
    if order.get("express"):
        total += base * (1.0 if zone_name == "intl" else 0.5)
    if order.get("fragile"):
        total += 5
    if order.get("remote") and zone_name != "intl":
        total += 8
    return total


def is_free(order, zone_name):
    return zone_name == "domestic" and not order.get("express") and order["subtotal"] >= 100


def shipping_cost(order):
    zone_name = zone(order["country"])
    base = base_price(zone_name, order["weight_kg"])
    extra = surcharges(order, zone_name, base)
    total = (0 if is_free(order, zone_name) else base) + extra
    total *= 1 - DISCOUNT.get(order.get("tier"), 0)
    if 0 < total < 3:
        total = 3
    return round(total, 2)
