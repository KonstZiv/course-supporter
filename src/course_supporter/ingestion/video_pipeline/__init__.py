"""Video ingestion pipeline (Phase 2.4) — new namespace, isolated from ``vd/``.

Skeleton (task 2.4.1) wires a 7-step video pipeline on offline stubs.
Real per-step logic lands in tasks 2.4.2-2.4.7; the legacy
``course_supporter.ingestion.video`` + ``course_supporter.vd`` modules are
removed in task 2.4.9. This package has zero import dependencies on
``course_supporter.vd``.
"""

from course_supporter.ingestion.video_pipeline.processor import VideoProcessor

__all__ = ["VideoProcessor"]
