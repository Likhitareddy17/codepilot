import pytest

from app.pagination import paginate

ITEMS = [1, 2, 3, 4, 5]


def test_second_page():
    assert paginate(ITEMS, page=2, page_size=2) == [3, 4]


def test_page_beyond_the_end_is_empty():
    assert paginate(ITEMS, page=10, page_size=2) == []


def test_invalid_page_arguments_are_rejected():
    with pytest.raises(ValueError):
        paginate(ITEMS, page=0, page_size=2)
    with pytest.raises(ValueError):
        paginate(ITEMS, page=1, page_size=0)
