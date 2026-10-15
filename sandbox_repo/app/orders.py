"""Order lookup and order totals."""

import logging

logger = logging.getLogger("order_service")

TAX_RATES = {"US": 0.08, "EU": 0.20, "UK": 0.20}

_ORDERS = {
    101: {"id": 101, "subtotal": 50.0, "total": 54.0, "region": "US"},
    102: {"id": 102, "subtotal": 100.0, "total": 120.0, "region": "EU"},
    103: {"id": 103, "subtotal": 80.0, "total": 96.0, "region": "UK"},
}


def find_order(order_id):
    """Return the order with this id, or None if there is no such order."""
    return _ORDERS.get(order_id)


def get_order_total(order_id):
    """Return the stored total of an order."""
    order = find_order(order_id)
    return order["total"]


def list_order_ids():
    """All known order ids, in ascending order."""
    return sorted(_ORDERS)


def calculate_order_total(order):
    """Return the order subtotal plus the tax for its region."""
    region = order.get("region", "US")
    tax_rate = TAX_RATES.get(region, 0.0)
    if tax_rate == 0.0:
        logger.warning("Tax rate lookup failed for region=%s, defaulting to 0", region)
    return order["subtotal"] * (1 + tax_rate)
