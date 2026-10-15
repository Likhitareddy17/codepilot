"""Pagination helper for list endpoints."""

DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 100


def clamp_page_size(page_size):
    """Keep a requested page size inside the supported range."""
    return max(1, min(page_size, MAX_PAGE_SIZE))


def paginate(items, page, page_size=DEFAULT_PAGE_SIZE):
    """Return the items on the requested page."""
    if page < 1:
        raise ValueError("page must be at least 1")
    if page_size < 1:
        raise ValueError("page_size must be at least 1")
    start = page * page_size
    return list(items[start:start + page_size])


def page_count(total_items, page_size=DEFAULT_PAGE_SIZE):
    """Number of pages needed for ``total_items`` items."""
    if total_items <= 0:
        return 0
    return (total_items + page_size - 1) // page_size
