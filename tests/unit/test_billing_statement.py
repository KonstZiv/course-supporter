"""The statement's table of lines, without a database (BIL-PAY).

The lock of completeness: every kind of movement the journal writes is
either a line of the statement or explicitly hidden — a new kind (the
parallel BIL-E work may add one) turns this red instead of vanishing from
the statement. Also the cursor's round trip.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from course_supporter.billing.kinds import (
    OUTFLOW_KINDS,
    BatchSource,
    MovementKind,
    RefundKind,
)
from course_supporter.billing.statement import (
    HIDDEN,
    LINES,
    QUALIFIED_BY_BATCH,
    QUALIFIED_BY_REFUND,
    InvalidCursorError,
    StatementLineKind,
    UnmappedMovementError,
    _decode,
    _encode,
    balance_effect,
    line_of,
)


def test_every_movement_kind_is_a_line_or_hidden() -> None:
    shown = {kind for kind, _ in LINES}
    assert shown.isdisjoint(HIDDEN)
    assert shown | HIDDEN == set(MovementKind), (
        f"not mapped: {set(MovementKind) - shown - HIDDEN}"
    )


def test_reserving_is_hidden_and_nothing_else_is() -> None:
    assert {MovementKind.RESERVE, MovementKind.RESERVE_RELEASE} == HIDDEN


@pytest.mark.parametrize("kind", sorted(QUALIFIED_BY_BATCH))
def test_a_credit_is_a_line_for_every_source_of_batch(kind: MovementKind) -> None:
    assert {q for k, q in LINES if k is kind} == set(BatchSource)


@pytest.mark.parametrize("kind", sorted(QUALIFIED_BY_REFUND))
def test_a_refunds_movement_is_a_line_for_every_kind_of_refund(
    kind: MovementKind,
) -> None:
    assert {q for k, q in LINES if k is kind} == set(RefundKind)


def test_the_other_movements_stand_alone() -> None:
    alone = set(MovementKind) - HIDDEN - QUALIFIED_BY_BATCH - QUALIFIED_BY_REFUND
    for kind in alone:
        assert {q for k, q in LINES if k is kind} == {None}, kind


def test_every_line_kind_is_reachable() -> None:
    assert set(LINES.values()) == set(StatementLineKind)


def test_the_line_kinds_are_the_screens_keys() -> None:
    assert {k.value for k in StatementLineKind} == {
        "purchase",
        "admin_credit",
        "charge",
        "benefit_covered",
        "platform_covered",
        "expired",
        "refund",
        "refund_hold",
        "refund_released",
        "dispute_hold",
        "dispute_released",
        "dispute_lost",
        "difference_arisen",
        "difference_repaid",
        "difference_annulled",
    }


@pytest.mark.parametrize(
    ("kind", "source", "refund", "line", "affects", "units"),
    [
        ("credit", "purchase", None, StatementLineKind.PURCHASE, True, 7),
        ("credit", "admin_credit", None, StatementLineKind.ADMIN_CREDIT, True, 7),
        ("debit", None, None, StatementLineKind.CHARGE, True, -7),
        ("batch_expired", "purchase", None, StatementLineKind.EXPIRED, True, -7),
        (
            "difference_repaid",
            "purchase",
            None,
            StatementLineKind.DIFFERENCE_REPAID,
            True,
            -7,
        ),
        ("refund_completed", "purchase", "refund", StatementLineKind.REFUND, True, -7),
        (
            "refund_completed",
            "purchase",
            "dispute",
            StatementLineKind.DISPUTE_LOST,
            True,
            -7,
        ),
        ("refund_hold", "purchase", "refund", StatementLineKind.REFUND_HOLD, False, 7),
        (
            "refund_hold",
            "purchase",
            "dispute",
            StatementLineKind.DISPUTE_HOLD,
            False,
            7,
        ),
        (
            "refund_rejected",
            "purchase",
            "refund",
            StatementLineKind.REFUND_RELEASED,
            False,
            7,
        ),
        (
            "refund_rejected",
            "purchase",
            "dispute",
            StatementLineKind.DISPUTE_RELEASED,
            False,
            7,
        ),
        ("benefit_covered", None, None, StatementLineKind.BENEFIT_COVERED, False, 7),
        ("platform_covered", None, None, StatementLineKind.PLATFORM_COVERED, False, 7),
        (
            "difference_arisen",
            None,
            None,
            StatementLineKind.DIFFERENCE_ARISEN,
            False,
            7,
        ),
        (
            "difference_annulled",
            None,
            None,
            StatementLineKind.DIFFERENCE_ANNULLED,
            False,
            7,
        ),
    ],
)
def test_line_of(
    kind: str,
    source: str | None,
    refund: str | None,
    line: StatementLineKind,
    affects: bool,
    units: int,
) -> None:
    """A movement of 7 units: signed when it changes the balance, else 7."""
    found = line_of(kind, source, refund)
    assert (found.kind, found.affects_balance, found.units(7)) == (line, affects, units)


def test_the_balance_is_changed_by_a_credit_and_the_outflows_only() -> None:
    """affects_balance is the journal's rule (kinds.py), for every line."""
    changing = {MovementKind.CREDIT} | OUTFLOW_KINDS
    for movement, qualifier in LINES:
        line = line_of(
            movement.value,
            qualifier.value if isinstance(qualifier, BatchSource) else "purchase",
            qualifier.value if isinstance(qualifier, RefundKind) else None,
        )
        assert line.affects_balance is (movement in changing), movement
        assert balance_effect(movement) == (
            +1 if movement is MovementKind.CREDIT else -1 if movement in changing else 0
        )


def test_a_lost_dispute_is_its_own_line() -> None:
    lost = line_of("refund_completed", "purchase", "dispute")
    refunded = line_of("refund_completed", "purchase", "refund")
    assert (lost.kind, refunded.kind) == (
        StatementLineKind.DISPUTE_LOST,
        StatementLineKind.REFUND,
    )


@pytest.mark.parametrize(
    ("kind", "source", "refund"),
    [("reserve", "purchase", None), ("credit", None, None), ("refund_hold", "x", None)],
)
def test_what_the_table_does_not_know_is_never_dropped(
    kind: str, source: str | None, refund: str | None
) -> None:
    with pytest.raises(UnmappedMovementError):
        line_of(kind, source, refund)


def test_the_cursor_goes_and_comes_back() -> None:
    at, movement = datetime(2026, 11, 15, 12, 30, 1, 123456, tzinfo=UTC), uuid.uuid4()
    assert _decode(_encode(at, movement)) == (at, movement)


@pytest.mark.parametrize(
    "cursor", ["", "x", "bm90IGpzb24", _encode(datetime(2026, 1, 1), uuid.uuid4())]
)
def test_a_cursor_not_given_is_refused(cursor: str) -> None:
    with pytest.raises(InvalidCursorError):
        _decode(cursor)
