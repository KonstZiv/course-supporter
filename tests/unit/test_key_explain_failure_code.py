"""BIL-E3, part B: the failure reason of a refused key explanation and its code.

The pair is fixed here: the worker writes ``funds refused: insufficient_funds``
when the funds port refuses the work for want of units, and the reference route
answers its ``failure_code`` with ``insufficient_units`` — both from the one
function and the one table of ``workers/key_explain.py``, never a copy of the
text. Any other failure reason has no code.
"""

from __future__ import annotations

import pytest

from course_supporter.billing.quotes import INSUFFICIENT_UNITS
from course_supporter.funds_port import FundsRefusalReason
from course_supporter.workers.key_explain import (
    FAILURE_CODES,
    failure_code_of,
    funds_refused_reason,
)


def test_the_refusal_for_want_of_units_and_its_code() -> None:
    reason = funds_refused_reason(FundsRefusalReason.INSUFFICIENT_FUNDS)

    assert reason == "funds refused: insufficient_funds", "the text stays as written"
    assert failure_code_of(reason) == "insufficient_units" == INSUFFICIENT_UNITS
    assert dict(FAILURE_CODES) == {reason: "insufficient_units"}


@pytest.mark.parametrize(
    "reason",
    [
        None,
        "the key or the task text changed before the explanations were written",
        "generation failed: the ladder ran out",
        "generation failed unexpectedly: ValueError: boom",
        "funds refused: None",
        "Funds refused: insufficient_funds",
    ],
)
def test_every_other_failure_has_no_code(reason: str | None) -> None:
    assert failure_code_of(reason) is None
