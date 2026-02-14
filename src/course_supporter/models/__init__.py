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
    SlideVideoMapEntry,
    WebReference,
)
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)

__all__ = [
    "ChunkType",
    "ConceptOutput",
    "ContentChunk",
    "CourseContext",
    "CourseStructure",
    "ExerciseOutput",
    "LessonOutput",
    "ModuleDifficulty",
    "ModuleOutput",
    "SlideRange",
    "SlideVideoMapEntry",
    "SourceDocument",
    "SourceType",
    "WebReference",
]
