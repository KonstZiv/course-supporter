"""How many reviews the doors let through (``billing.outlook.reviews_left``)."""

from __future__ import annotations

import pytest

from course_supporter.billing.outlook import reviews_left


@pytest.mark.parametrize(
    ("available", "price", "fact", "expected"),
    [
        (500, 256, 105, 3),
        (500, 192, 88, 4),
        (500, 189, 49, 7),
        (395, 256, 105, 2),
        (185, 256, 105, 0),
        (256, 256, 105, 1),
        (255, 256, 105, 0),
    ],
)
def test_reviews_left(available: int, price: int, fact: int, expected: int) -> None:
    assert reviews_left(available, price, fact) == expected
