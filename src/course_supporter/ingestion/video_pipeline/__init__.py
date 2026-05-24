"""Video ingestion pipeline (Phase 2.4) — namespace isolated from the former ``vd/``.

Skeleton (task 2.4.1) wired a 7-step video pipeline on offline stubs;
real per-step logic landed in tasks 2.4.2-2.4.7. The legacy
``course_supporter.ingestion.video`` + ``course_supporter.vd`` modules were
removed in task 2.4.9A. This package never depended on
``course_supporter.vd``.
"""

from course_supporter.ingestion.video_pipeline.processor import VideoProcessor

__all__ = ["VideoProcessor"]
