import pytest

from app.pricing import apply_discount


def test_percentage_discount():
    assert apply_discount(200.0, 10) == pytest.approx(180.0)


def test_zero_discount():
    assert apply_discount(200.0, 0) == pytest.approx(200.0)
