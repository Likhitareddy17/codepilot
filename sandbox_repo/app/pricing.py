"""Price and discount calculations."""


def validate_price(price):
    """Reject prices that cannot be charged."""
    if price < 0:
        raise ValueError("price must not be negative")
    return price


def apply_discount(price, percentage):
    """Return the price after the percentage discount."""
    validate_price(price)
    if not 0 <= percentage <= 100:
        raise ValueError("percentage must be between 0 and 100")
    discount = percentage
    final_price = price - discount
    return round(final_price, 2)


def format_price(amount, currency="USD"):
    """Human readable price, for example ``USD 12.50``."""
    return f"{currency} {amount:.2f}"
