"""L3 (к.3): every authored-document response schema carries a non-null
``processing_phase`` (Рат.3, acceptance #2)."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from course_supporter.api.schemas import (
    AuthoredDocumentCreateResponse,
    AuthoredDocumentResponse,
    AuthoredDocumentSummaryResponse,
)

_SCHEMAS = [
    AuthoredDocumentSummaryResponse,
    AuthoredDocumentResponse,
    AuthoredDocumentCreateResponse,
]


def _orm_like() -> SimpleNamespace:
    """A from_attributes source carrying the union of the three schemas' fields."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        course_node_id=uuid.uuid4(),
        course_root_id=uuid.uuid4(),
        source_type="web",
        material_role="educational",
        task_type=None,
        source_url="https://example.com/x",
        filename=None,
        language=None,
        order=0,
        state="pending",
        processing_phase="processing",
        error_message=None,
        error_category=None,
        job_id=uuid.uuid4(),
        warnings=[],
        processing_estimate=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("schema", _SCHEMAS)
def test_schema_declares_processing_phase(schema: type[BaseModel]) -> None:
    assert "processing_phase" in schema.model_fields


@pytest.mark.parametrize("schema", _SCHEMAS)
def test_processing_phase_populated_from_attributes(schema: type[BaseModel]) -> None:
    model = schema.model_validate(_orm_like())
    value = model.processing_phase  # type: ignore[attr-defined]
    assert value == "processing"
    assert value  # never null/empty (Рат.3)
