"""Which materials a student may see — one rule for every reader (task 10).

Moved here from the portal routes (``api/routes/_portal_shared`` and the
course tree in ``portal_courses``), below both layers, so the storage layer
takes the SAME rules without importing a route module. Two rules, one per
axis of a document:

* its ROLE — :func:`role_visible_to_student`;
* its processing STATE — :func:`state_visible_to_student` (the course tree
  shows a document only once it is ready).

The portal checks each loaded document with these predicates; the concept
search filters in SQL by :func:`student_visible_roles` and
:func:`student_visible_states`, which ask the same predicates — never a second
copy of a condition. The portal's behaviour is unchanged by the move.
"""

from __future__ import annotations

from course_supporter.models.source import MaterialRole, MaterialState


def role_visible_to_student(material_role: str) -> bool:
    """Whether a material of this ROLE may be seen by a student in the portal.

    Allowlist by design: visible ⇔ the role is exactly ``educational``. A
    methodological material (mentor-check instructions, reference solutions) is
    hidden from students on every portal surface that addresses a document.

    Written as an allowlist, NOT ``!= 'methodological'``, on purpose: when a
    third role is later added to the enum it stays hidden until a visibility
    rule is chosen for it explicitly, rather than leaking to students by
    default. The completeness test
    (``test_portal_role_visibility.py``) pins one assertion per enum member, so
    a new role cannot enter the vocabulary without a deliberate decision here.

    Scoped to the material's ROLE only — the name says "role", not "visibility"
    in general, so the future publication predicate (DD-6-F) sits beside this
    rather than dissolving into it. Compares against the canonical
    :class:`MaterialRole` member the author side writes (``update_material_role``
    stores ``MaterialRole.<X>.value`` into the ``str`` column), never a bare
    string literal.
    """
    return material_role == MaterialRole.EDUCATIONAL.value


def student_visible_roles() -> list[str]:
    """Every role :func:`role_visible_to_student` lets a student see.

    The same rule asked of each role in the vocabulary, for a reader that
    filters in SQL (``material_role IN (...)``) rather than per loaded
    document. Asked at call time, so the list cannot drift from the rule; a
    value outside :class:`MaterialRole` is never in it — hidden, as the rule's
    allowlist hides it.
    """
    return [role.value for role in MaterialRole if role_visible_to_student(role.value)]


def state_visible_to_student(material_state: str) -> bool:
    """Whether the course tree shows a document in this processing STATE.

    Allowlist, like the role rule: shown ⇔ the state is exactly ``ready``. A
    document being (re)processed (``pending``) or failed (``error``) is left
    out of the student's tree until it is ready again. The state itself is
    derived by ``orm.AuthoredDocument.state`` — on a loaded document, or in
    SQL on the class.
    """
    return material_state == MaterialState.READY.value


def student_visible_states() -> list[str]:
    """Every state :func:`state_visible_to_student` shows a student.

    The SQL-side form of the rule (``state IN (...)``), asked of each member
    of :class:`MaterialState` at call time, as :func:`student_visible_roles`
    is.
    """
    return [
        state.value for state in MaterialState if state_visible_to_student(state.value)
    ]
