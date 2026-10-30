"""The concept adapter of the course-link finder, end to end (task 10, commit K3).

Requires PostgreSQL (``--run-db``). One course, the task on lesson 2b:

    root
    ├── block 1           (order 1)
    │   └── lesson 1a     (order 1)  far from the task
    └── block 2           (order 2)
        ├── lesson 2a     (order 1)  sibling of the task's node
        └── lesson 2b     (order 2)  the task's node; the task document has order 5

Learning order: lesson 1a, block 1, lesson 2a, lesson 2b (documents of order
below 5, the task, then the rest), block 2, root. So lesson 1a, block 1,
lesson 2a and the low documents of lesson 2b are earlier than the task; block
2 and the root — later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.homework.course_links import (
    EXCERPT_CHARS,
    MAX_LINKS,
    ConceptCourseLinkFinder,
    FailedCriterion,
    LinkCandidate,
    SubmissionScope,
    build_course_link_finder,
)
from course_supporter.storage.document_segment_repository import (
    DocumentSegmentRepository,
)
from course_supporter.storage.orm import CourseNode, DocumentSegment, Tenant
from tests.integration.test_document_segment_repository import (
    _NOT_A_CANDIDATE,
    _seed_case,
    _seed_child,
    _seed_material,
    _seed_root,
    _seed_segment,
    _seed_summary,
    _seed_taught,
)

pytestmark = pytest.mark.requires_db

_TASK_ORDER = 5


@dataclass(frozen=True)
class _Course:
    tenant: Tenant
    root: CourseNode
    block_1: CourseNode
    lesson_1a: CourseNode
    block_2: CourseNode
    lesson_2a: CourseNode
    lesson_2b: CourseNode
    scope: SubmissionScope


@pytest.fixture()
async def course(db_session: AsyncSession, seed_tenant: Tenant) -> _Course:
    root = await _seed_root(db_session, seed_tenant)
    block_2 = await _seed_child(db_session, seed_tenant, root, order=2)
    block_1 = await _seed_child(db_session, seed_tenant, root, order=1)
    lesson_1a = await _seed_child(db_session, seed_tenant, block_1, order=1)
    lesson_2b = await _seed_child(db_session, seed_tenant, block_2, order=2)
    lesson_2a = await _seed_child(db_session, seed_tenant, block_2, order=1)
    task = await _seed_material(
        db_session, lesson_2b, root, task_type="task", order=_TASK_ORDER
    )
    return _Course(
        tenant=seed_tenant,
        root=root,
        block_1=block_1,
        lesson_1a=lesson_1a,
        block_2=block_2,
        lesson_2a=lesson_2a,
        lesson_2b=lesson_2b,
        scope=SubmissionScope(
            tenant_id=seed_tenant.id,
            course_root_id=root.id,
            task_node_id=lesson_2b.id,
            task_document_id=task.id,
        ),
    )


async def _taught(
    session: AsyncSession,
    course: _Course,
    node: CourseNode,
    concepts: list[str],
    *,
    order: int = 0,
    **segment: Any,
) -> DocumentSegment:
    """A material on ``node`` with one segment whose MAIN concepts are ``concepts``."""
    doc = await _seed_material(session, node, course.root, order=order)
    summary = await _seed_summary(session, doc, course.root)
    return await _seed_segment(session, summary, course.root, main=concepts, **segment)


async def _links(
    session: AsyncSession, course: _Course, *concepts: str
) -> tuple[LinkCandidate, ...]:
    """The links of one failed criterion with ``concepts``."""
    found = await ConceptCourseLinkFinder(session).find(
        course.scope, [FailedCriterion(id="c1", text="—", concepts=concepts)]
    )
    return found["c1"]


def _ids(links: tuple[LinkCandidate, ...]) -> list[Any]:
    return [link.segment_id for link in links]


class TestConceptKey:
    """Lock 3: a concept matches by its normalisation key, on both sides."""

    @pytest.mark.parametrize(
        ("criterion", "segment"),
        [
            ("list comprehension", "list comprehension"),  # exact
            ("List Comprehension", "list comprehension"),  # case
            ("list-comprehension", "list comprehension"),  # hyphen
            ("list comprehensions", "list comprehension"),  # plural s
            ("list comprehension", "List-Comprehensions"),  # all three, segment side
        ],
    )
    async def test_spelling_variant_finds_the_segment(
        self, db_session: AsyncSession, course: _Course, criterion: str, segment: str
    ) -> None:
        seg = await _taught(db_session, course, course.lesson_2a, [segment])
        await _taught(db_session, course, course.lesson_2a, ["dict comprehension"])

        links = await _links(db_session, course, criterion)

        assert _ids(links) == [seg.id]
        assert links[0].matched_concepts == (criterion,)

    async def test_every_id_answered_and_no_concepts_no_links(
        self, db_session: AsyncSession, course: _Course
    ) -> None:
        """No concepts or no match — an empty tuple, never a missing key."""
        seg = await _taught(db_session, course, course.lesson_2a, ["recursion"])

        found = await build_course_link_finder(db_session).find(
            course.scope,
            [
                FailedCriterion(id="c1", text="a", concepts=("Recursion",)),
                FailedCriterion(id="c2", text="b", concepts=()),
                FailedCriterion(id="c3", text="c", concepts=("closure",)),
            ],
        )

        assert {key: _ids(value) for key, value in found.items()} == {
            "c1": [seg.id],
            "c2": [],
            "c3": [],
        }


class TestSelection:
    """Lock 4: at most three; earlier before later; coverage, then the tree."""

    async def test_at_most_three(
        self, db_session: AsyncSession, course: _Course
    ) -> None:
        for order in range(MAX_LINKS + 2):
            await _taught(db_session, course, course.lesson_2a, ["loop"], order=order)

        assert len(await _links(db_session, course, "loop")) == MAX_LINKS

    async def test_earlier_ones_shut_out_the_later(
        self, db_session: AsyncSession, course: _Course
    ) -> None:
        """One earlier match is enough for none of the later ones to appear.

        The later ones are closer in the tree (the task's own node after the
        task, its parent) — and still left out.
        """
        earlier = await _taught(db_session, course, course.lesson_1a, ["loop"])
        await _taught(db_session, course, course.lesson_2b, ["loop"], order=9)
        await _taught(db_session, course, course.block_2, ["loop"])

        links = await _links(db_session, course, "loop")

        assert _ids(links) == [earlier.id]
        assert links[0].later_in_course is False

    async def test_later_ones_only_when_nothing_earlier(
        self, db_session: AsyncSession, course: _Course
    ) -> None:
        """No earlier match: the later ones, marked as later in the course."""
        own_after = await _taught(
            db_session, course, course.lesson_2b, ["loop"], order=_TASK_ORDER + 1
        )
        parent = await _taught(db_session, course, course.block_2, ["loop"])
        await _taught(db_session, course, course.lesson_1a, ["range"])

        links = await _links(db_session, course, "loop")

        assert _ids(links) == [own_after.id, parent.id]
        assert all(link.later_in_course for link in links)

    async def test_more_concepts_covered_ranks_higher(
        self, db_session: AsyncSession, course: _Course
    ) -> None:
        """Both concepts, far from the task, above one concept in the task's node."""
        one = await _taught(db_session, course, course.lesson_2b, ["loop"])
        both = await _taught(db_session, course, course.lesson_1a, ["loop", "range"])

        links = await _links(db_session, course, "loop", "range")

        assert _ids(links) == [both.id, one.id]
        assert [link.matched_concepts for link in links] == [
            ("loop", "range"),
            ("loop",),
        ]
        assert 0.0 < links[1].confidence < links[0].confidence <= 1.0

    async def test_same_node_above_far(
        self, db_session: AsyncSession, course: _Course
    ) -> None:
        far = await _taught(db_session, course, course.lesson_1a, ["loop"])
        same = await _taught(db_session, course, course.lesson_2b, ["loop"])

        assert _ids(await _links(db_session, course, "loop")) == [same.id, far.id]

    async def test_earlier_closer_to_the_task_ranks_higher(
        self, db_session: AsyncSession, course: _Course
    ) -> None:
        """Same node, same coverage: the one met last before the task first."""
        first = await _taught(db_session, course, course.lesson_1a, ["loop"], order=0)
        second = await _taught(db_session, course, course.lesson_1a, ["loop"], order=1)

        links = await _links(db_session, course, "loop")

        assert _ids(links) == [second.id, first.id]


class TestExcerpt:
    async def test_cyrillic_excerpt_is_exactly_the_limit(
        self, db_session: AsyncSession, course: _Course
    ) -> None:
        content = "Генератор списку будує новий список з умовою. " * 12
        assert len(content) > EXCERPT_CHARS
        await _taught(db_session, course, course.lesson_2a, ["loop"], content=content)

        (link,) = await _links(db_session, course, "loop")

        assert link.excerpt is not None
        assert len(link.excerpt) == EXCERPT_CHARS
        assert link.excerpt == content[:EXCERPT_CHARS]
        assert link.excerpt.encode("utf-8").decode("utf-8") == link.excerpt


class TestCandidateFilters:
    """The port's candidates are ``search_by_concepts``'s."""

    @pytest.mark.parametrize("case", sorted(_NOT_A_CANDIDATE))
    async def test_not_a_candidate_is_not_linked(
        self, db_session: AsyncSession, course: _Course, case: str
    ) -> None:
        """E.g. a methodological material teaching the same concept is not linked.

        Each case of the search's own candidate table, and the two readers
        agree on what they find.
        """
        candidate = await _seed_case(db_session, course.root, {})
        await _seed_case(db_session, course.root, _NOT_A_CANDIDATE[case])

        links = await _links(db_session, course, "nav")
        hits = await DocumentSegmentRepository(db_session).search_by_concepts(
            course_root_id=course.root.id, concepts=["nav"]
        )

        assert _ids(links) == [candidate.id]
        assert [hit.segment_id for hit in hits] == [candidate.id]

    async def test_list_candidates_in_the_search_order(
        self, db_session: AsyncSession, course: _Course
    ) -> None:
        """The candidates come in the learning order the search returns."""
        for node in (course.root, course.block_2, course.lesson_1a, course.lesson_2a):
            await _seed_taught(db_session, node, course.root, segments=2)

        repo = DocumentSegmentRepository(db_session)
        candidates = await repo.list_candidates(course_root_id=course.root.id)
        hits = await repo.search_by_concepts(
            course_root_id=course.root.id, concepts=["nav"]
        )

        assert [s.segment_id for s in candidates.segments] == [
            h.segment_id for h in hits
        ]
        assert len(hits) == 8
