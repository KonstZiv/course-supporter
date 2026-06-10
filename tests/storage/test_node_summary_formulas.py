"""NodeSummaryRaw / NodeSummaryFinal content_hash formula tests (Phase 3.1).

Covers Phase 3.1 acceptance #4 — the load-bearing invariant that
``content_hash`` excludes ``enclosing_context`` for both Raw and
Final (vision §3 KD9 table lines 729-730). Two NodeSummary* rows
that differ ONLY in ``enclosing_context`` must produce byte-
identical content_hashes; any other content-field change must flip
the hash.

Mirrors ``test_content_hash_formulas.py`` shape (AsyncMock session,
``_make_X`` builders, focused per-property classes).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from course_supporter.storage.content_hash import ContentHashService
from course_supporter.storage.orm import NodeSummaryFinal, NodeSummaryRaw


def _make_raw(**overrides: Any) -> NodeSummaryRaw:
    """Build a NodeSummaryRaw with sane defaults; override individual fields."""
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "course_node_id": uuid.uuid4(),
        "title": "Node Title",
        "description": "Node description",
        "learning_objectives": [],
        "knowledge": [],
        "skills": [],
        "success_criteria": [],
        "assessment_approach": "",
        "teaching_approach": "",
        "key_activities": [],
        "common_mistakes": [],
        "main_concepts": [],
        "secondary_concepts": [],
        "compressed_summary": "",
        "enclosing_context": None,
        "methodist_observations": [],
        "own_documents_count": 0,
        "own_chars_count": 0,
        "cumulative_documents_count": 0,
        "cumulative_chars_count": 0,
    }
    defaults.update(overrides)
    raw = NodeSummaryRaw(**defaults)
    raw.deleted_at = None
    return raw


def _make_final(**overrides: Any) -> NodeSummaryFinal:
    """Build a NodeSummaryFinal with sane defaults; override individual fields."""
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "course_node_id": uuid.uuid4(),
        "title": "Node Title",
        "description": "Node description",
        "learning_objectives": [],
        "knowledge": [],
        "skills": [],
        "success_criteria": [],
        "assessment_approach": "",
        "teaching_approach": "",
        "key_activities": [],
        "common_mistakes": [],
        "main_concepts": [],
        "secondary_concepts": [],
        "enclosing_context": None,
        "is_manual": False,
        "manual_description": None,
        "own_documents_count": 0,
        "own_chars_count": 0,
        "cumulative_documents_count": 0,
        "cumulative_chars_count": 0,
    }
    defaults.update(overrides)
    final = NodeSummaryFinal(**defaults)
    final.deleted_at = None
    return final


# ── NodeSummaryRaw formula ───────────────────────────────────────


class TestNodeSummaryRawFormula:
    """``NodeSummaryRaw.content_hash`` covers all content fields EXCEPT
    ``enclosing_context`` (vision §3 KD9). Concept lists are
    order-independent; other list fields preserve insertion order.
    """

    async def test_deterministic_across_calls(self) -> None:
        raw = _make_raw(title="Lesson 1", main_concepts=["loops"])
        svc = ContentHashService(AsyncMock())
        first = await svc._compute_hash_for(raw, exclude_ids=None)
        second = await svc._compute_hash_for(raw, exclude_ids=None)
        assert first == second

    async def test_enclosing_context_excluded_from_hash(self) -> None:
        # Phase 3.1 acceptance #4 — the load-bearing invariant.
        cnid = uuid.uuid4()
        a = _make_raw(course_node_id=cnid, enclosing_context=None)
        b = _make_raw(course_node_id=cnid, enclosing_context="ancestor chain context")
        # Equalize the id so only enclosing_context differs (id is not
        # part of the hash formula, but kept aligned for clarity).
        b.id = a.id
        svc = ContentHashService(AsyncMock())
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) == await svc._compute_hash_for(b, exclude_ids=None)

    async def test_main_concepts_order_independence(self) -> None:
        a = _make_raw(main_concepts=["alpha", "beta", "gamma"])
        b = _make_raw(main_concepts=["gamma", "alpha", "beta"])
        b.id = a.id
        b.course_node_id = a.course_node_id
        svc = ContentHashService(AsyncMock())
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) == await svc._compute_hash_for(b, exclude_ids=None)

    async def test_title_change_flips_hash(self) -> None:
        a = _make_raw(title="First")
        b = _make_raw(title="Second")
        b.id = a.id
        b.course_node_id = a.course_node_id
        svc = ContentHashService(AsyncMock())
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) != await svc._compute_hash_for(b, exclude_ids=None)

    async def test_methodist_observations_change_flips_hash(self) -> None:
        # Raw-only field — must contribute to the hash, not be elided.
        a = _make_raw(methodist_observations=[])
        b = _make_raw(methodist_observations=["observation one"])
        b.id = a.id
        b.course_node_id = a.course_node_id
        svc = ContentHashService(AsyncMock())
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) != await svc._compute_hash_for(b, exclude_ids=None)

    async def test_compressed_summary_change_flips_hash(self) -> None:
        a = _make_raw(compressed_summary="")
        b = _make_raw(compressed_summary="summary")
        b.id = a.id
        b.course_node_id = a.course_node_id
        svc = ContentHashService(AsyncMock())
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) != await svc._compute_hash_for(b, exclude_ids=None)


# ── NodeSummaryFinal formula ──────────────────────────────────────


class TestNodeSummaryFinalFormula:
    """``NodeSummaryFinal.content_hash`` covers all content fields EXCEPT
    ``enclosing_context`` (vision §3 KD9); ``compressed_summary`` is NOT
    persisted on Final (vision §267); ``methodist_observations`` is
    Raw-only. ``is_manual`` and ``manual_description`` are author
    content and DO contribute to the hash.
    """

    async def test_deterministic_across_calls(self) -> None:
        final = _make_final(title="Lesson 1")
        svc = ContentHashService(AsyncMock())
        first = await svc._compute_hash_for(final, exclude_ids=None)
        second = await svc._compute_hash_for(final, exclude_ids=None)
        assert first == second

    async def test_enclosing_context_excluded_from_hash(self) -> None:
        # Phase 3.1 acceptance #4 — Final mirror of the Raw invariant.
        cnid = uuid.uuid4()
        a = _make_final(course_node_id=cnid, enclosing_context=None)
        b = _make_final(course_node_id=cnid, enclosing_context="ancestor chain context")
        b.id = a.id
        svc = ContentHashService(AsyncMock())
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) == await svc._compute_hash_for(b, exclude_ids=None)

    async def test_is_manual_flip_changes_hash(self) -> None:
        # Author-state flag — toggling it must flip the hash because
        # it changes the canonical author-content of an empty leaf.
        a = _make_final(is_manual=False)
        b = _make_final(is_manual=True)
        b.id = a.id
        b.course_node_id = a.course_node_id
        svc = ContentHashService(AsyncMock())
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) != await svc._compute_hash_for(b, exclude_ids=None)

    async def test_manual_description_change_flips_hash(self) -> None:
        a = _make_final(manual_description=None)
        b = _make_final(manual_description="hand-written description")
        b.id = a.id
        b.course_node_id = a.course_node_id
        svc = ContentHashService(AsyncMock())
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) != await svc._compute_hash_for(b, exclude_ids=None)


# ── Hash-graph topology (Q-A) ─────────────────────────────────────


class TestNodeSummaryLeafTopology:
    """NodeSummaryRaw / Final are LEAVES of the content_hash graph
    (Q-A ratified at Phase 3.1 pre-flight). ``_fetch_parent`` returns
    ``None`` for both; ``invalidate_up`` recomputes own hash and stops.
    """

    @pytest.mark.parametrize(
        "make",
        [_make_raw, _make_final],
        ids=["raw", "final"],
    )
    async def test_fetch_parent_returns_none(self, make: Any) -> None:
        entity = make()
        svc = ContentHashService(AsyncMock())
        parent = await svc._fetch_parent(entity)
        assert parent is None
