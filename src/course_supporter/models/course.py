"""Course structure schemas."""

from datetime import datetime

from pydantic import BaseModel, Field

from course_supporter.models.source import SourceDocument


class SlideVideoMapEntry(BaseModel):
    """Pydantic mirror of ORM SlideVideoMapping.

    Maps slide_number to video_timecode (e.g., "01:23:45").
    Matches ORM: String(20) for video_timecode.
    """

    slide_number: int
    video_timecode: str


class CourseContext(BaseModel):
    """Unified context for course structuring.

    Combines all processed source documents and optional
    slide-video mappings into a single object for the
    ArchitectAgent.
    """

    documents: list[SourceDocument]
    slide_video_mappings: list[SlideVideoMapEntry] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
