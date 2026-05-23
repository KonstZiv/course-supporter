"""Per-entity content_hash formula tests (vision §3 KD9, Phase 1 commit (e)).

Phase 1 extends ``DocumentSummary`` and ``DocumentSegment`` formulas
with own content fields per Gap 2 of the sprint deferred debt. Three
properties are locked here:

1. **Determinism** — the same inputs always produce the same hash
   (no time-dependent or insertion-order-dependent state leaks in).
2. **Order-independence on concept lists** — LLM output order for
   ``main_concepts`` / ``secondary_concepts`` is non-deterministic;
   ``sorted(...)`` at the formula boundary normalizes it so a producer
   reshuffle does not spuriously invalidate every downstream Merkle
   ancestor.
3. **Field sensitivity** — every included field (``content`` /
   ``title`` / ``description`` / either concept list) actually
   contributes to the digest; mutating any one of them flips the
   resulting hash.

Tests run against the real ``ContentHashService._compute_hash_for``
with an in-memory ``AsyncMock`` session. ``DocumentSegment`` has no
children so its formula is fully covered without DB; ``DocumentSummary``
mocks the segments query to assert children-aggregation is preserved
post-extension.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.storage.content_hash import (
    ContentHashService,
    _encode_local_fields,
    compute_content_hash,
)
from course_supporter.storage.orm import DocumentSegment, DocumentSummary


def _make_segment(
    *,
    content: str = "lesson body",
    main_concepts: list[str] | None = None,
    secondary_concepts: list[str] | None = None,
) -> DocumentSegment:
    seg = DocumentSegment(
        id=uuid.uuid4(),
        document_summary_id=uuid.uuid4(),
        course_root_id=uuid.uuid4(),
        order=0,
        start_pos=0,
        end_pos=len(content),
        content=content,
        main_concepts=list(main_concepts) if main_concepts is not None else [],
        secondary_concepts=(
            list(secondary_concepts) if secondary_concepts is not None else []
        ),
        content_char_count=len(content),
    )
    seg.deleted_at = None
    return seg


def _make_summary(
    *,
    title: str = "Lesson 1",
    description: str | None = "first lesson",
    main_concepts: list[str] | None = None,
    secondary_concepts: list[str] | None = None,
) -> DocumentSummary:
    summary = DocumentSummary(
        id=uuid.uuid4(),
        authored_document_id=uuid.uuid4(),
        course_root_id=uuid.uuid4(),
        title=title,
        description=description,
        main_concepts=list(main_concepts) if main_concepts is not None else [],
        secondary_concepts=(
            list(secondary_concepts) if secondary_concepts is not None else []
        ),
        content_char_count=None,
        status="ready",
    )
    summary.deleted_at = None
    return summary


def _service_with_segment_children(child_hashes: list[str]) -> ContentHashService:
    """Build a ContentHashService whose session returns ``child_hashes``
    when ``_compute_hash_for(DocumentSummary)`` queries segments.
    """
    session: Any = AsyncMock()
    result_mock = MagicMock()
    # `(h,) for h in ...` mirrors the row-tuple shape returned by
    # ``select(DocumentSegment.content_hash)`` queries.
    result_mock.all.return_value = [(h,) for h in child_hashes]
    session.execute.return_value = result_mock
    return ContentHashService(session)


# ── DocumentSegment formula ───────────────────────────────────────


class TestDocumentSegmentFormula:
    """``DocumentSegment.content_hash`` extends over ``content`` + sorted
    concept lists; ``content_char_count`` is excluded (derived from
    ``content``).
    """

    async def test_deterministic_across_calls(self) -> None:
        seg = _make_segment(
            content="some content",
            main_concepts=["loops", "variables"],
            secondary_concepts=["scope"],
        )
        svc = ContentHashService(AsyncMock())
        first = await svc._compute_hash_for(seg, exclude_ids=None)
        second = await svc._compute_hash_for(seg, exclude_ids=None)
        assert first == second

    async def test_concept_order_independence_main(self) -> None:
        a = _make_segment(main_concepts=["alpha", "beta", "gamma"])
        b = _make_segment(main_concepts=["gamma", "alpha", "beta"])
        # Equalize content + secondary so only main_concepts ordering differs.
        b.id = a.id  # not used by formula but kept consistent
        b.content = a.content
        b.secondary_concepts = list(a.secondary_concepts or [])
        svc = ContentHashService(AsyncMock())
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) == await svc._compute_hash_for(b, exclude_ids=None)

    async def test_concept_order_independence_secondary(self) -> None:
        a = _make_segment(secondary_concepts=["x", "y", "z"])
        b = _make_segment(secondary_concepts=["z", "x", "y"])
        b.content = a.content
        b.main_concepts = list(a.main_concepts or [])
        svc = ContentHashService(AsyncMock())
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) == await svc._compute_hash_for(b, exclude_ids=None)

    async def test_content_change_flips_hash(self) -> None:
        a = _make_segment(content="first")
        b = _make_segment(content="second")
        b.main_concepts = list(a.main_concepts or [])
        b.secondary_concepts = list(a.secondary_concepts or [])
        svc = ContentHashService(AsyncMock())
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) != await svc._compute_hash_for(b, exclude_ids=None)

    async def test_main_concepts_change_flips_hash(self) -> None:
        a = _make_segment(main_concepts=["loops"])
        b = _make_segment(main_concepts=["recursion"])
        b.content = a.content
        b.secondary_concepts = list(a.secondary_concepts or [])
        svc = ContentHashService(AsyncMock())
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) != await svc._compute_hash_for(b, exclude_ids=None)

    async def test_secondary_concepts_change_flips_hash(self) -> None:
        a = _make_segment(secondary_concepts=["scope"])
        b = _make_segment(secondary_concepts=["closure"])
        b.content = a.content
        b.main_concepts = list(a.main_concepts or [])
        svc = ContentHashService(AsyncMock())
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) != await svc._compute_hash_for(b, exclude_ids=None)

    async def test_main_vs_secondary_swap_flips_hash(self) -> None:
        """Concepts are not interchangeable across roles; a value
        moving from main to secondary must change the hash even though
        the union set is identical.
        """
        a = _make_segment(main_concepts=["loops"], secondary_concepts=["scope"])
        b = _make_segment(main_concepts=["scope"], secondary_concepts=["loops"])
        b.content = a.content
        svc = ContentHashService(AsyncMock())
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) != await svc._compute_hash_for(b, exclude_ids=None)

    async def test_char_count_excluded(self) -> None:
        """``content_char_count`` is derived from ``content``; mutating it
        without changing ``content`` must NOT change the hash.
        """
        a = _make_segment(content="abc")
        b = _make_segment(content="abc")
        a.content_char_count = 3
        b.content_char_count = 999  # simulated drift; formula must ignore
        svc = ContentHashService(AsyncMock())
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) == await svc._compute_hash_for(b, exclude_ids=None)

    async def test_empty_visual_content_byte_identical_to_pre_2_4_6(self) -> None:
        """D1 hard-gate (task 2.4.6): visual_content empty (``[]``) or absent
        (``None``, in-memory) must NOT perturb the hash of any existing
        segment. The hash must equal the pre-2.4.6 formula
        ``compute_content_hash(content + sorted(concepts))`` byte-for-byte,
        so every non-video / visual-less row keeps its stored content_hash.
        """
        seg_none = _make_segment(
            content="body",
            main_concepts=["b", "a"],
            secondary_concepts=["c"],
        )
        seg_empty = _make_segment(
            content="body",
            main_concepts=["b", "a"],
            secondary_concepts=["c"],
        )
        seg_empty.visual_content = []  # flushed not-null default shape

        expected_pre_2_4_6 = compute_content_hash(
            _encode_local_fields(
                {
                    "content": "body",
                    "main_concepts": sorted(["b", "a"]),
                    "secondary_concepts": ["c"],
                }
            ),
            [],
        )
        svc = ContentHashService(AsyncMock())
        assert seg_none.visual_content is None  # _make_segment leaves it unset
        assert (
            await svc._compute_hash_for(seg_none, exclude_ids=None)
            == expected_pre_2_4_6
        )
        assert (
            await svc._compute_hash_for(seg_empty, exclude_ids=None)
            == expected_pre_2_4_6
        )

    async def test_non_empty_visual_content_changes_hash(self) -> None:
        """A populated visual stream (video) joins the formula, so a segment
        with visuals hashes differently from the same transcript without.
        """
        without = _make_segment(content="body")
        with_visual = _make_segment(content="body")
        with_visual.main_concepts = list(without.main_concepts or [])
        with_visual.secondary_concepts = list(without.secondary_concepts or [])
        with_visual.visual_content = [
            {
                "position_ms": 0,
                "description": "slide A",
                "kind": "anchor",
                "scene_id": 0,
            }
        ]
        svc = ContentHashService(AsyncMock())
        assert await svc._compute_hash_for(
            without, exclude_ids=None
        ) != await svc._compute_hash_for(with_visual, exclude_ids=None)

    async def test_visual_content_order_sensitive(self) -> None:
        """Frame order is temporally meaningful (NOT sorted, unlike concept
        sets): reordering the visual refs must flip the hash.
        """
        ref_a = {
            "position_ms": 0,
            "description": "slide A",
            "kind": "anchor",
            "scene_id": 0,
        }
        ref_b = {
            "position_ms": 5000,
            "description": "slide B",
            "kind": "diff",
            "scene_id": 1,
        }
        forward = _make_segment(content="body")
        reversed_ = _make_segment(content="body")
        reversed_.main_concepts = list(forward.main_concepts or [])
        reversed_.secondary_concepts = list(forward.secondary_concepts or [])
        forward.visual_content = [ref_a, ref_b]
        reversed_.visual_content = [ref_b, ref_a]
        svc = ContentHashService(AsyncMock())
        assert await svc._compute_hash_for(
            forward, exclude_ids=None
        ) != await svc._compute_hash_for(reversed_, exclude_ids=None)


# ── DocumentSummary formula ───────────────────────────────────────


class TestDocumentSummaryFormula:
    """``DocumentSummary.content_hash`` extends over ``title`` +
    ``description`` + sorted concept lists; child segment hashes are
    aggregated via the existing Merkle infrastructure unchanged.
    """

    async def test_deterministic_across_calls(self) -> None:
        summary = _make_summary(
            title="Lesson 1",
            description="intro",
            main_concepts=["a", "b"],
        )
        svc = _service_with_segment_children([])
        first = await svc._compute_hash_for(summary, exclude_ids=None)
        second = await svc._compute_hash_for(summary, exclude_ids=None)
        assert first == second

    async def test_title_change_flips_hash(self) -> None:
        a = _make_summary(title="A")
        b = _make_summary(title="B")
        b.description = a.description
        b.main_concepts = list(a.main_concepts or [])
        b.secondary_concepts = list(a.secondary_concepts or [])
        svc = _service_with_segment_children([])
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) != await svc._compute_hash_for(b, exclude_ids=None)

    async def test_description_change_flips_hash(self) -> None:
        a = _make_summary(description="first")
        b = _make_summary(description="second")
        b.title = a.title
        b.main_concepts = list(a.main_concepts or [])
        b.secondary_concepts = list(a.secondary_concepts or [])
        svc = _service_with_segment_children([])
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) != await svc._compute_hash_for(b, exclude_ids=None)

    async def test_description_null_vs_empty_string(self) -> None:
        """``description = None`` and ``description = ""`` must collapse
        to the same hash. Vision §2.2 marks description as optional;
        a producer that omits it (NULL at DB level) and a producer that
        sends an empty string should not diverge downstream.
        """
        a = _make_summary(description=None)
        b = _make_summary(description="")
        b.title = a.title
        b.main_concepts = list(a.main_concepts or [])
        b.secondary_concepts = list(a.secondary_concepts or [])
        svc = _service_with_segment_children([])
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) == await svc._compute_hash_for(b, exclude_ids=None)

    async def test_concept_order_independence(self) -> None:
        a = _make_summary(main_concepts=["alpha", "beta", "gamma"])
        b = _make_summary(main_concepts=["gamma", "alpha", "beta"])
        b.title = a.title
        b.description = a.description
        b.secondary_concepts = list(a.secondary_concepts or [])
        svc = _service_with_segment_children([])
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) == await svc._compute_hash_for(b, exclude_ids=None)

    async def test_main_concepts_change_flips_hash(self) -> None:
        a = _make_summary(main_concepts=["loops"])
        b = _make_summary(main_concepts=["recursion"])
        b.title = a.title
        b.description = a.description
        b.secondary_concepts = list(a.secondary_concepts or [])
        svc = _service_with_segment_children([])
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) != await svc._compute_hash_for(b, exclude_ids=None)

    async def test_segment_child_hash_propagates(self) -> None:
        """Adding / changing a child segment hash must change the
        summary hash (Merkle aggregation per KD9 unchanged by the
        commit (e) extension).
        """
        summary = _make_summary()
        svc_no_children = _service_with_segment_children([])
        svc_with_child = _service_with_segment_children(["a" * 64])
        no_children_hash = await svc_no_children._compute_hash_for(
            summary, exclude_ids=None
        )
        with_child_hash = await svc_with_child._compute_hash_for(
            summary, exclude_ids=None
        )
        assert no_children_hash != with_child_hash

    async def test_child_segment_order_independent(self) -> None:
        """``compute_content_hash`` already sorts child hashes; the
        summary hash must not depend on the segments query result order.
        """
        summary = _make_summary()
        svc_a = _service_with_segment_children(["a" * 64, "b" * 64])
        svc_b = _service_with_segment_children(["b" * 64, "a" * 64])
        ha = await svc_a._compute_hash_for(summary, exclude_ids=None)
        hb = await svc_b._compute_hash_for(summary, exclude_ids=None)
        assert ha == hb

    async def test_main_vs_secondary_swap_flips_hash(self) -> None:
        a = _make_summary(main_concepts=["loops"], secondary_concepts=["scope"])
        b = _make_summary(main_concepts=["scope"], secondary_concepts=["loops"])
        b.title = a.title
        b.description = a.description
        svc = _service_with_segment_children([])
        assert await svc._compute_hash_for(
            a, exclude_ids=None
        ) != await svc._compute_hash_for(b, exclude_ids=None)


# ── Cross-entity drift detection ─────────────────────────────────


@pytest.mark.parametrize(
    "factory",
    [_make_segment, _make_summary],
    ids=["segment", "summary"],
)
class TestFormulaReturnShape:
    """Both extended formulas return the canonical 64-char lowercase
    hex digest expected by the rest of the Merkle infrastructure.
    """

    async def test_returns_64_char_lowercase_hex(self, factory: Any) -> None:
        entity = factory()
        if isinstance(entity, DocumentSummary):
            svc = _service_with_segment_children([])
        else:
            svc = ContentHashService(AsyncMock())
        result = await svc._compute_hash_for(entity, exclude_ids=None)
        assert len(result) == 64
        assert result == result.lower()
        # Lowercase hex alphabet only.
        assert all(c in "0123456789abcdef" for c in result)


# ── Unicode invariant (ensure_ascii=False) ───────────────────────


class TestUnicodeContentInvariant:
    """Lock ``ensure_ascii=False`` invariant for non-ASCII content (vision §3 KD9).

    ``_encode_local_fields`` uses ``json.dumps(..., ensure_ascii=False)``
    (``content_hash.py:127-129``) so Cyrillic / accented characters contribute
    to the digest as raw UTF-8 bytes, not ``\\uXXXX`` escapes. Without this
    flag, identical content from a UA/RU course would hash differently
    depending on encoding-aware vs encoding-naive producers, breaking
    cache invariance across the Merkle tree.
    """

    async def test_unicode_content_distinct_and_stable(self) -> None:
        """Cyrillic content yields a stable hash distinct from ASCII transliteration."""
        svc = ContentHashService(AsyncMock())
        cyrillic = _make_segment(content="Привіт світ")
        ascii_sub = _make_segment(content="Privit svit")

        h_cyrillic = await svc._compute_hash_for(cyrillic, exclude_ids=None)
        h_ascii = await svc._compute_hash_for(ascii_sub, exclude_ids=None)
        h_cyrillic_again = await svc._compute_hash_for(cyrillic, exclude_ids=None)

        # Unicode bytes contribute to digest (not stripped to escapes).
        assert h_cyrillic != h_ascii
        # Stable across calls — determinism holds for non-ASCII payloads.
        assert h_cyrillic == h_cyrillic_again
