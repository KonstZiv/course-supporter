"""Root-CourseNode test factory (task 2.4.13B fixture-fallout hotfix).

Single point of truth for the **root** branch of test
``CourseNode`` setup. After task 2.4.13 landed CHECK constraint
``course_nodes_root_language_required`` (``parent_id IS NOT NULL OR
default_language IS NOT NULL``), any fixture or test setup that
builds a root ``CourseNode`` (``parent_id=None``) without
``default_language`` violates the CHECK on commit/flush. Per
2.4.13 рішення 1, that is the intended behaviour — root without
explicit language is data the system must reject.

This factory exists so the dozen test-setup sites that need a
root node express that intent uniformly (default canonical
``"ukr"`` mirroring the migration backfill in
``phase24_root_lang_required``) and so a future fourteenth test
cannot quietly recreate the bug by forgetting the field. The
factory mirrors task 2.4.13 рішення 5 («helper — єдина точка
мовної логіки») at the test-fixture layer.

Consumers:

- ``tests/integration/conftest.py`` — central ``seed_root_node`` /
  ``committed_seeds`` fixtures (high-leverage path: many failing
  tests reach the CHECK through these fixtures, not through a
  direct ``CourseNode()``).
- ``tests/integration/test_content_hash_persistence.py``,
  ``tests/integration/test_cost_endpoints_db.py``,
  ``tests/integration/test_external_service_call_db.py``,
  ``tests/integration/test_job_cancellation_service_db.py``,
  ``tests/integration/test_job_redesign_db.py``,
  ``tests/integration/test_material_node_repository_db.py``,
  ``tests/storage/test_authored_document_repository.py``,
  ``tests/storage/test_cascade_invalidation.py``,
  ``tests/storage/test_cascade_kd_alpha.py``,
  ``tests/storage/test_get_subtree_active_documents.py``,
  ``tests/storage/test_get_subtree_tenant_isolation.py`` — files
  with their own root-setup constructions.

Scope discipline (task 2.4.13B рішення 2-3):

- **ROOT ONLY.** Child nodes (``parent_id`` set) satisfy the
  CHECK structurally and do not need a language — per task 2.4.13
  child ``default_language`` is dead data. Callers that build
  children keep using raw ``CourseNode(...)``; this factory does
  not have a child counterpart on purpose (minimum surface).
- **NOT for subject-under-test.** Unit tests that exercise the
  ``CourseNode`` model itself (``tests/unit/test_material_node.py``,
  ``tests/unit/test_tenant_isolation.py``) construct
  ``CourseNode`` directly as the subject of the assertion, never
  commit, and so never trip the CHECK. They stay raw — wrapping
  them via this factory would obscure what they are testing.

Update this module on contract changes (e.g. ``default_language``
column rename or a different canonical default); all consumer
sites pick up the new shape atomically.
"""

from __future__ import annotations

from typing import Any

from course_supporter.storage.orm import CourseNode


def make_root_course_node(
    *,
    tenant_id: Any,
    default_language: str = "ukr",
    **overrides: Any,
) -> CourseNode:
    """Build a root ``CourseNode`` instance with ``default_language`` set.

    Returns an unflushed, unattached SQLAlchemy instance — caller
    decides session, ``session.add`` / ``session.flush`` / commit
    discipline so this helper composes with both savepoint-only
    (``db_session`` / ``seed_root_node``) and committed
    (``committed_seeds``) fixture patterns.

    Args:
        tenant_id: Owning ``Tenant.id``. Required keyword.
        default_language: Canonical ISO 639-3 code. Defaults to
            ``"ukr"`` to match the migration backfill in
            ``phase24_root_lang_required`` (the same default the
            production whitelist treats as the canonical Ukrainian
            form). Override per-call when the test needs a
            specific language (e.g. ``default_language="eng"`` for
            an English-course scenario).
        **overrides: Any other ``CourseNode`` constructor kwarg
            (``id``, ``title``, ``description``, ``order``,
            ``content_hash``). ``parent_id`` is fixed to ``None``
            — by construction this is a root factory; if a test
            needs a child node it should use ``CourseNode(...)``
            directly with the parent it cares about.

    Raises:
        TypeError: when ``overrides`` carries ``parent_id`` —
            callers asking for a child have reached the wrong
            factory and we surface that loudly rather than
            silently flipping the node to a child.
    """
    if "parent_id" in overrides:
        raise TypeError(
            "make_root_course_node does not accept parent_id — this "
            "factory is root-specific (task 2.4.13B рішення 2). For a "
            "child node, construct CourseNode(...) directly with the "
            "parent_id you need; the CHECK constraint does not apply "
            "to child nodes."
        )
    return CourseNode(
        tenant_id=tenant_id,
        parent_id=None,
        default_language=default_language,
        **overrides,
    )
