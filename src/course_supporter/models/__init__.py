"""Pydantic schemas for course-supporter domain models."""

from course_supporter.models.course import (
    ConceptOutput,
    CourseContext,
    CourseStructure,
    ExerciseOutput,
    LessonOutput,
    ModuleDifficulty,
    ModuleOutput,
    SlideRange,
    SlideTimecodeRef,
    SlideVideoMapEntry,
    WebReference,
)
from course_supporter.models.outline import (
    CodeSnippet,
    MaterialOutline,
    OutlineSection,
    PresenterInfo,
)
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)

__all__ = [
    "ChunkType",
    "CodeSnippet",
    "ConceptOutput",
    "ContentChunk",
    "CourseContext",
    "CourseStructure",
    "ExerciseOutput",
    "LessonOutput",
    "MaterialOutline",
    "ModuleDifficulty",
    "ModuleOutput",
    "OutlineSection",
    "PresenterInfo",
    "SlideRange",
    "SlideTimecodeRef",
    "SlideVideoMapEntry",
    "SourceDocument",
    "SourceType",
    "WebReference",
]
