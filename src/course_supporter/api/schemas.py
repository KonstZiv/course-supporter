"""Request/response schemas for the API layer."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from course_supporter.models.course import SlideVideoMapEntry

# --- Course ---


class CourseCreateRequest(BaseModel):
    """Request body for POST /courses."""

    title: str = Field(..., min_length=1, max_length=500)
    description: str | None = None


class CourseResponse(BaseModel):
    """Response for course creation and listing."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None
    created_at: datetime
    updated_at: datetime


# --- Slide-Video Mapping ---


class SlideVideoMapRequest(BaseModel):
    """Request body for POST /courses/{id}/slide-mapping."""

    mappings: list[SlideVideoMapEntry] = Field(..., min_length=1)


class SlideVideoMapItemResponse(BaseModel):
    """Single slide-video mapping in response."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slide_number: int
    video_timecode: str


class SlideVideoMapResponse(BaseModel):
    """Response for slide-video mapping creation."""

    created: int
    mappings: list[SlideVideoMapItemResponse]


# --- Course Detail (nested) ---


class ExerciseResponse(BaseModel):
    """Exercise within a lesson."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    description: str
    reference_solution: str | None
    grading_criteria: str | None
    difficulty_level: int | None


class ConceptResponse(BaseModel):
    """Concept card within a lesson."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    definition: str
    examples: list[str] | None
    timecodes: list[str] | None
    slide_references: list[int] | None
    web_references: list[dict[str, str]] | None


class LessonResponse(BaseModel):
    """Lesson within a module."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    order: int
    video_start_timecode: str | None
    video_end_timecode: str | None
    slide_range: dict[str, int] | None
    concepts: list[ConceptResponse]
    exercises: list[ExerciseResponse]


class ModuleResponse(BaseModel):
    """Module within a course."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None
    learning_goal: str | None
    difficulty: str | None
    order: int
    lessons: list[LessonResponse]


class SourceMaterialResponse(BaseModel):
    """Source material attached to a course."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_type: str
    source_url: str
    filename: str | None
    status: str
    created_at: datetime


class CourseDetailResponse(BaseModel):
    """Full course detail with nested structure."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None
    learning_goal: str | None
    created_at: datetime
    updated_at: datetime
    modules: list[ModuleResponse]
    source_materials: list[SourceMaterialResponse]


class LessonDetailResponse(BaseModel):
    """Lesson detail with concepts and exercises."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    order: int
    video_start_timecode: str | None
    video_end_timecode: str | None
    slide_range: dict[str, int] | None
    concepts: list[ConceptResponse]
    exercises: list[ExerciseResponse]


# --- Materials ---


class MaterialCreateResponse(BaseModel):
    """Response for material creation."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_type: str
    source_url: str
    filename: str | None
    status: str
    created_at: datetime
    job_id: uuid.UUID | None = None
