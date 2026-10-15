import pytest

from app.orders import calculate_order_total, find_order, get_order_total


def test_existing_order():
    assert find_order(101)["id"] == 101
    assert get_order_total(101) == 54.0


def test_missing_order():
    assert get_order_total(404) is None


def test_order_total():
    order = {"id": 7, "subtotal": 100, "region": "us"}
    assert calculate_order_total(order) == pytest.approx(108.0)
