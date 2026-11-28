"""Deleting a student's account at the student's request (Б-2, terms 1.2).

The administrator's command is ``scripts/student_admin.py delete``; what it
does and does not do is in ``README.md`` next to this file.
"""

from course_supporter.student_deletion.service import (
    DELETED_TEXT,
    BatchRefund,
    DeletionRefusedError,
    DeletionReport,
    PlaceCount,
    StorageCleanup,
    delete_student,
    finish_storage,
    neutral_key,
    neutral_name,
)

__all__ = [
    "DELETED_TEXT",
    "BatchRefund",
    "DeletionRefusedError",
    "DeletionReport",
    "PlaceCount",
    "StorageCleanup",
    "delete_student",
    "finish_storage",
    "neutral_key",
    "neutral_name",
]
