"""Tests for MergeStep."""

from course_supporter.ingestion.merge import MergeStep
from course_supporter.models.course import (
    CourseContext,
    MaterialNodeSummary,
)
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)


def _make_doc(
    source_type: SourceType = SourceType.TEXT,
    chunks: list[ContentChunk] | None = None,
) -> SourceDocument:
    """Create a minimal SourceDocument for testing."""
    return SourceDocument(
        source_type=source_type,
        source_url=f"file:///test.{source_type}",
        title=f"Test {source_type}",
        chunks=chunks or [],
    )


class TestMergeStep:
    def test_single_document(self) -> None:
        """One document -> CourseContext with 1 document."""
        step = MergeStep()
        doc = _make_doc()

        result = step.merge([doc])

        assert isinstance(result, CourseContext)
        assert len(result.documents) == 1
        assert result.documents[0].source_type == SourceType.TEXT

    def test_multiple_documents(self) -> None:
        """Video + text -> CourseContext with 2 documents."""
        step = MergeStep()
        video = _make_doc(SourceType.VIDEO)
        text = _make_doc(SourceType.TEXT)

        result = step.merge([video, text])

        assert len(result.documents) == 2

    def test_empty_documents_returns_empty_context(self) -> None:
        """Empty documents list returns an empty CourseContext.

        Intermediate parent nodes that rely on child snapshots have no own
        materials to merge; upstream guards (``_collect_ready_documents``)
        enforce non-empty for leaf generation where materials are required.
        """
        step = MergeStep()

        result = step.merge([])

        assert result.documents == []
        assert result.slide_video_mappings == []
        assert result.material_tree == []

    def test_empty_documents_preserves_material_tree(self) -> None:
        """An empty merge still carries the ``material_tree`` forward.

        The parent-generate path passes a tree summary so the Architect
        can still orient itself via the hierarchy even without own
        materials.
        """
        import uuid as _uuid

        step = MergeStep()
        tree = [
            MaterialNodeSummary(
                node_id=_uuid.uuid4(),
                title="Intro",
                parent_id=None,
                depth=0,
                order=0,
                material_refs=[],
            )
        ]

        result = step.merge([], material_tree=tree)

        assert result.documents == []
        assert len(result.material_tree) == 1
        assert result.material_tree[0].title == "Intro"

    def test_document_ordering(self) -> None:
        """Documents sorted by priority: video -> presentation -> text -> web."""
        step = MergeStep()
        web = _make_doc(SourceType.WEB)
        video = _make_doc(SourceType.VIDEO)
        text = _make_doc(SourceType.TEXT)
        presentation = _make_doc(SourceType.PRESENTATION)

        result = step.merge([web, text, presentation, video])

        types = [d.source_type for d in result.documents]
        assert types == [
            SourceType.VIDEO,
            SourceType.PRESENTATION,
            SourceType.TEXT,
            SourceType.WEB,
        ]

    def test_no_mappings_default(self) -> None:
        """No mappings -> empty list in CourseContext."""
        step = MergeStep()

        result = step.merge([_make_doc()])

        assert result.slide_video_mappings == []

    def test_stable_sort_same_type(self) -> None:
        """Multiple documents of the same type preserve relative order."""
        step = MergeStep()
        video_a = _make_doc(SourceType.VIDEO)
        video_a = video_a.model_copy(update={"title": "Video A"})
        video_b = _make_doc(SourceType.VIDEO)
        video_b = video_b.model_copy(update={"title": "Video B"})
        text = _make_doc(SourceType.TEXT)

        result = step.merge([video_a, text, video_b])

        assert result.documents[0].title == "Video A"
        assert result.documents[1].title == "Video B"
        assert result.documents[2].source_type == SourceType.TEXT

    def test_created_at_set(self) -> None:
        """CourseContext has created_at timestamp."""
        step = MergeStep()

        result = step.merge([_make_doc()])

        assert result.created_at is not None

    def test_no_material_tree_default(self) -> None:
        """No material_tree -> empty list in CourseContext."""
        step = MergeStep()

        result = step.merge([_make_doc()])

        assert result.material_tree == []

    def test_with_material_tree(self) -> None:
        """material_tree passed through to CourseContext."""
        step = MergeStep()
        tree = [
            MaterialNodeSummary(
                title="Module 1",
                order=0,
                material_titles=["lecture.pdf"],
                children=[
                    MaterialNodeSummary(
                        title="Lesson 1",
                        order=0,
                        material_titles=["video.mp4"],
                    ),
                ],
            ),
        ]

        result = step.merge([_make_doc()], material_tree=tree)

        assert len(result.material_tree) == 1
        assert result.material_tree[0].title == "Module 1"
        assert result.material_tree[0].children[0].title == "Lesson 1"

    def test_material_tree_none_becomes_empty(self) -> None:
        """material_tree=None defaults to empty list."""
        step = MergeStep()

        result = step.merge([_make_doc()], material_tree=None)

        assert result.material_tree == []

    def test_visual_scene_chunks_pass_through(self) -> None:
        """VISUAL_SCENE chunks from VD pipeline preserved in merge."""
        step = MergeStep()
        video = _make_doc(
            SourceType.VIDEO,
            chunks=[
                ContentChunk(
                    chunk_type=ChunkType.TRANSCRIPT,
                    text="Hello world",
                    index=0,
                    start_sec=0.0,
                    end_sec=10.0,
                ),
                ContentChunk(
                    chunk_type=ChunkType.VISUAL_SCENE,
                    text="Code editor with Python function",
                    index=0,
                    start_sec=0.0,
                    end_sec=10.0,
                    metadata={
                        "scene_id": 0,
                        "scene_type": "screen_recording",
                        "importance": 4,
                        "topics": ["python", "functions"],
                    },
                ),
            ],
        )
        text = _make_doc(SourceType.TEXT)

        result = step.merge([text, video])

        # Video first (priority), both chunk types preserved
        assert result.documents[0].source_type == SourceType.VIDEO
        chunks = result.documents[0].chunks
        assert len(chunks) == 2
        types = {c.chunk_type for c in chunks}
        assert ChunkType.TRANSCRIPT in types
        assert ChunkType.VISUAL_SCENE in types

        vd_chunk = next(c for c in chunks if c.chunk_type == ChunkType.VISUAL_SCENE)
        assert vd_chunk.start_sec == 0.0
        assert vd_chunk.metadata["scene_type"] == "screen_recording"
        assert vd_chunk.metadata["topics"] == ["python", "functions"]
