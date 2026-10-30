"""Completeness lock for the portal role-visibility allowlist (step A, P3).

Pins one explicit visibility verdict per :class:`MaterialRole` member. A role
added to the vocabulary without a decision here turns this test red — that is
the point: the allowlist in :func:`role_visible_to_student` is a forced
decision at the enum, not a silent default that leaks a new role to students.

The course tree's state rule (:func:`state_visible_to_student`, task 10) is
locked the same way, one verdict per :class:`MaterialState` member.
"""

from __future__ import annotations

from course_supporter.material_visibility import (
    role_visible_to_student,
    state_visible_to_student,
    student_visible_roles,
    student_visible_states,
)
from course_supporter.models.source import MaterialRole, MaterialState

# Explicit visibility verdict per role. Adding a MaterialRole member WITHOUT a
# row here fails ``test_every_role_has_an_explicit_verdict`` — a deliberate
# decision is required before a new role can ship.
_EXPECTED_VISIBILITY: dict[MaterialRole, bool] = {
    MaterialRole.EDUCATIONAL: True,
    MaterialRole.METHODOLOGICAL: False,
}


def test_every_role_has_an_explicit_verdict() -> None:
    """Every enum member is covered by the expected-visibility table."""
    assert set(_EXPECTED_VISIBILITY) == set(MaterialRole)


def test_predicate_matches_expected_verdict() -> None:
    """The predicate agrees with the explicit verdict for every role."""
    for role, visible in _EXPECTED_VISIBILITY.items():
        assert role_visible_to_student(role.value) is visible


def test_unknown_role_value_is_hidden() -> None:
    """A value outside the vocabulary is hidden (allowlist safety-by-default)."""
    assert role_visible_to_student("some_future_role") is False


def test_sql_side_roles_are_the_rule_asked_per_role() -> None:
    """The SQL allowlist the search filters by is the predicate, per role."""
    assert student_visible_roles() == [
        role.value for role, visible in _EXPECTED_VISIBILITY.items() if visible
    ]


_EXPECTED_STATE_VISIBILITY: dict[MaterialState, bool] = {
    MaterialState.READY: True,
    MaterialState.PENDING: False,
    MaterialState.ERROR: False,
}


def test_every_state_has_an_explicit_verdict() -> None:
    """Every document state is covered by the expected-visibility table."""
    assert set(_EXPECTED_STATE_VISIBILITY) == set(MaterialState)


def test_state_predicate_matches_expected_verdict() -> None:
    """The tree shows a document only when it is ready; unknown is hidden."""
    for state, visible in _EXPECTED_STATE_VISIBILITY.items():
        assert state_visible_to_student(state.value) is visible
    assert state_visible_to_student("some_future_state") is False


def test_sql_side_states_are_the_rule_asked_per_state() -> None:
    """The SQL allowlist of states is the predicate, per state."""
    assert student_visible_states() == [
        state.value for state, visible in _EXPECTED_STATE_VISIBILITY.items() if visible
    ]
