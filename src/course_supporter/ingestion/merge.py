"""Merge step for combining processed source documents into CourseContext."""

from __future__ import annotations

import structlog

from course_supporter.models.course import CourseContext, CourseNodeSummary
from course_supporter.models.source import SourceDocument, SourceType

logger = structlog.get_logger()

# Priority order for document types (lower index = higher priority)
SOURCE_TYPE_PRIORITY: dict[SourceType, int] = {
    SourceType.VIDEO: 0,
    SourceType.PRESENTATION: 1,
    SourceType.TEXT: 2,
    SourceType.WEB: 3,
}


class MergeStep:
    """Merge multiple SourceDocuments into a unified CourseContext.

    Responsibilities:
    1. Sort documents by source type priority (video first, web last)
    2. Package everything into CourseContext for ArchitectAgent

    This is a synchronous, pure data transformation — no I/O, no LLM.
    """

    def merge(
        self,
        documents: list[SourceDocument],
        material_tree: list[CourseNodeSummary] | None = None,
    ) -> CourseContext:
        """Merge source documents into CourseContext.

        Accepts an empty ``documents`` list: intermediate parent nodes that
        delegate their context to child snapshots have no own materials to
        merge, and the caller supplies the context via ``material_tree`` +
        out-of-band ``children_snapshots``. Upstream callers that require
        materials (leaf generation) guard with
        ``_collect_ready_documents(..., allow_empty=False)`` before reaching
        this step.

        Args:
            documents: List of processed SourceDocuments. May be empty.
            material_tree: Optional tree hierarchy with material associations.

        Returns:
            CourseContext with sorted documents and tree metadata.
        """
        if not documents:
            logger.info(
                "merge_empty_documents",
                tree_node_count=len(material_tree) if material_tree else 0,
            )
            return CourseContext(
                documents=[],
                material_tree=material_tree or [],
            )

        sorted_docs = sorted(
            documents,
            key=lambda d: SOURCE_TYPE_PRIORITY.get(d.source_type, 99),
        )

        logger.info(
            "merge_complete",
            document_count=len(sorted_docs),
            source_types=[str(d.source_type) for d in sorted_docs],
        )

        return CourseContext(
            documents=sorted_docs,
            material_tree=material_tree or [],
        )
