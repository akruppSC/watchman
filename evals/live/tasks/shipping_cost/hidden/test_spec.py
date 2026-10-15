import pytest
from app.shipping import shipping_cost


def o(**kw):
    return {"country": "US", "weight_kg": 1.0, "subtotal": 20.0, **kw}


@pytest.mark.parametrize("country,weight,expected", [
    ("US", 0.5, 4), ("US", 0.51, 7), ("US", 2, 7), ("US", 5, 12), ("US", 20, 25),
    ("DE", 0.5, 9), ("FR", 2, 15), ("ES", 5, 24), ("IT", 20, 48), ("NL", 1, 15),
    ("JP", 0.5, 14), ("CA", 2, 24), ("BR", 5, 40), ("AU", 20, 80),
])
def test_base_price_by_zone_and_weight(country, weight, expected):
    assert shipping_cost(o(country=country, weight_kg=weight)) == expected


@pytest.mark.parametrize("weight", [0, -1])
def test_non_positive_weight(weight):
    with pytest.raises(ValueError, match="invalid weight"):
        shipping_cost(o(weight_kg=weight))


def test_too_heavy():
    with pytest.raises(ValueError, match="too heavy"):
        shipping_cost(o(weight_kg=20.01))


def test_hazmat_rules():
    assert shipping_cost(o(hazmat=True)) == 22
    assert shipping_cost(o(country="DE", hazmat=True)) == 30
    with pytest.raises(ValueError, match="hazmat not allowed"):
        shipping_cost(o(country="JP", hazmat=True))


def test_express_surcharge():
    assert shipping_cost(o(express=True)) == 10.5
    assert shipping_cost(o(country="DE", express=True)) == 22.5
    assert shipping_cost(o(country="JP", express=True, weight_kg=2)) == 48


def test_fragile_and_remote():
    assert shipping_cost(o(fragile=True)) == 12
    assert shipping_cost(o(remote=True)) == 15
    assert shipping_cost(o(country="DE", remote=True)) == 23
    assert shipping_cost(o(country="JP", remote=True)) == 24
    assert shipping_cost(o(fragile=True, remote=True, hazmat=True, express=True)) == 7 + 15 + 3.5 + 5 + 8


def test_free_domestic_shipping():
    assert shipping_cost(o(subtotal=100)) == 0
    assert shipping_cost(o(subtotal=99.99)) == 7
    assert shipping_cost(o(subtotal=100, express=True)) == 10.5
    assert shipping_cost(o(subtotal=500, country="DE")) == 15
    assert shipping_cost(o(subtotal=100, fragile=True, remote=True)) == 13


def test_tier_discounts_apply_after_surcharges():
    assert shipping_cost(o(tier="silver")) == 6.65
    assert shipping_cost(o(tier="gold", fragile=True)) == 10.8
    assert shipping_cost(o(tier="unknown")) == 7


def test_minimum_fee():
    assert shipping_cost(o(weight_kg=0.1, tier="gold", subtotal=10)) == 3.6
    assert shipping_cost(o(subtotal=100, tier="gold")) == 0
    assert shipping_cost(o(subtotal=100, weight_kg=1, tier="gold", remote=False)) == 0


def test_rounding():
    assert shipping_cost(o(country="DE", weight_kg=5, tier="silver", fragile=True)) == 27.55
