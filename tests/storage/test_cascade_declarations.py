"""Verify ``__cascades_soft_delete_to__`` declarations match PHASE.md §1.2.

Phase 1 commit (c) wires direct cascade descendants on five soft-deletable
entry-point types per the §1.2 cascade declaration audit. These are pure
class-attribute assertions — no DB, no ORM session — so they catch any
accidental drift between the PHASE.md spec and the live ORM module without
needing fixtures.
"""

from course_supporter.storage.orm import (
    APIKey,
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
    HomeworkSubmission,
    Student,
    Tenant,
)


class TestCascadeDeclarations:
    """Each entry-point type declares the §1.2 direct descendants."""

    def test_tenant_cascades(self) -> None:
        assert Tenant.__cascades_soft_delete_to__ == [
            APIKey,
            CourseNode,
            Student,
            HomeworkSubmission,
        ]

    def test_course_node_cascades(self) -> None:
        # Self-reference enables recursive course-tree traversal in
        # CascadeDeleteService.build_cascade_map (storage/cascade.py).
        assert CourseNode.__cascades_soft_delete_to__ == [
            CourseNode,
            AuthoredDocument,
        ]

    def test_authored_document_cascades(self) -> None:
        assert AuthoredDocument.__cascades_soft_delete_to__ == [DocumentSummary]

    def test_document_summary_cascades(self) -> None:
        assert DocumentSummary.__cascades_soft_delete_to__ == [DocumentSegment]

    def test_student_cascades(self) -> None:
        assert Student.__cascades_soft_delete_to__ == [HomeworkSubmission]

    def test_terminal_types_have_no_descendants(self) -> None:
        # Leaves of the cascade graph: declarations either absent or empty.
        # Per §1.3, APIKey + DocumentSegment + HomeworkSubmission carry no
        # KD3 content scrub responsibility AND no further cascade — they
        # are terminal under their respective entry points.
        assert getattr(APIKey, "__cascades_soft_delete_to__", []) == []
        assert getattr(DocumentSegment, "__cascades_soft_delete_to__", []) == []
        assert getattr(HomeworkSubmission, "__cascades_soft_delete_to__", []) == []
