"""Merge step for combining processed source documents into CourseContext."""

from __future__ import annotations

import structlog

from course_supporter.models.course import (
    CourseContext,
    MaterialNodeSummary,
    SlideTimecodeRef,
)
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)

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
    2. Cross-reference slide<->video via SlideTimecodeRef mappings
    3. Package everything into CourseContext for ArchitectAgent

    This is a synchronous, pure data transformation — no I/O, no LLM.
    """

    def merge(
        self,
        documents: list[SourceDocument],
        mappings: list[SlideTimecodeRef] | None = None,
        material_tree: list[MaterialNodeSummary] | None = None,
    ) -> CourseContext:
        """Merge source documents and optional mappings into CourseContext.

        Args:
            documents: List of processed SourceDocuments.
            mappings: Optional slide<->video mappings for cross-referencing.
            material_tree: Optional tree hierarchy with material associations.

        Returns:
            CourseContext with sorted documents, mappings, and tree metadata.

        Raises:
            ValueError: If documents list is empty.
        """
        if not documents:
            raise ValueError("Cannot merge empty documents list")

        resolved_mappings = mappings or []

        # Sort documents by source_type priority
        sorted_docs = sorted(
            documents,
            key=lambda d: SOURCE_TYPE_PRIORITY.get(d.source_type, 99),
        )

        # Cross-reference slides with video timecodes
        if resolved_mappings:
            sorted_docs = self._apply_cross_references(sorted_docs, resolved_mappings)

        logger.info(
            "merge_complete",
            document_count=len(sorted_docs),
            mapping_count=len(resolved_mappings),
            source_types=[str(d.source_type) for d in sorted_docs],
        )

        return CourseContext(
            documents=sorted_docs,
            slide_video_mappings=resolved_mappings,
            material_tree=material_tree or [],
        )

    @staticmethod
    def _apply_cross_references(
        documents: list[SourceDocument],
        mappings: list[SlideTimecodeRef],
    ) -> list[SourceDocument]:
        """Enrich presentation slide chunks with video timecode references.

        For each SLIDE_TEXT chunk in presentation documents, if a mapping
        exists for that slide_number, add video_timecode to chunk metadata.

        Args:
            documents: Sorted list of SourceDocuments.
            mappings: Slide-to-video timecode mappings.

        Returns:
            Documents with enriched presentation chunks.
        """
        # Build lookup: slide_number -> video_timecode_start
        timecode_map: dict[int, str] = {
            m.slide_number: m.video_timecode_start for m in mappings
        }

        if not timecode_map:
            return documents

        enriched: list[SourceDocument] = []

        for doc in documents:
            if doc.source_type != SourceType.PRESENTATION:
                enriched.append(doc)
                continue

            new_chunks: list[ContentChunk] = []
            for chunk in doc.chunks:
                if chunk.chunk_type == ChunkType.SLIDE_TEXT:
                    slide_num = chunk.metadata.get("slide_number")
                    if slide_num is not None and slide_num in timecode_map:
                        updated_metadata = {
                            **chunk.metadata,
                            "video_timecode": timecode_map[slide_num],
                        }
                        new_chunks.append(
                            chunk.model_copy(update={"metadata": updated_metadata})
                        )
                        continue
                new_chunks.append(chunk)

            enriched.append(doc.model_copy(update={"chunks": new_chunks}))

        return enriched
