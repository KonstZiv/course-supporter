"""Completeness lock for the portal role-visibility allowlist (step A, P3).

Pins one explicit visibility verdict per :class:`MaterialRole` member. A role
added to the vocabulary without a decision here turns this test red — that is
the point: the allowlist in :func:`role_visible_to_student` is a forced
decision at the enum, not a silent default that leaks a new role to students.
"""

from __future__ import annotations

from course_supporter.api.routes._portal_shared import role_visible_to_student
from course_supporter.models.source import MaterialRole

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
