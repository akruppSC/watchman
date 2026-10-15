import pytest
from app.shipping import shipping_cost


def order(**kw):
    return {"country": "US", "weight_kg": 1, "subtotal": 20, **kw}


def test_base_prices():
    assert shipping_cost(order()) == 7
    assert shipping_cost(order(country="DE", weight_kg=0.4)) == 9
    assert shipping_cost(order(country="JP", weight_kg=20)) == 80


def test_invalid_weights():
    with pytest.raises(ValueError, match="invalid weight"):
        shipping_cost(order(weight_kg=0))
    with pytest.raises(ValueError, match="too heavy"):
        shipping_cost(order(weight_kg=20.1))


def test_hazmat():
    assert shipping_cost(order(hazmat=True)) == 22
    with pytest.raises(ValueError, match="hazmat not allowed"):
        shipping_cost(order(country="JP", hazmat=True))


def test_express_fragile_remote():
    assert shipping_cost(order(express=True)) == 10.5
    assert shipping_cost(order(country="JP", express=True)) == 48
    assert shipping_cost(order(fragile=True, remote=True)) == 20
    assert shipping_cost(order(country="JP", remote=True)) == 24


def test_free_shipping_and_discount():
    assert shipping_cost(order(subtotal=100)) == 0
    assert shipping_cost(order(subtotal=100, fragile=True)) == 5
    assert shipping_cost(order(tier="gold")) == 6.3
