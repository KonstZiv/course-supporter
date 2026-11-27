"""Unit tests for ``path_runner._after_delivery`` log privacy (DD-SP-DO)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import structlog
from pydantic import BaseModel, ValidationError
from structlog.testing import capture_logs

from course_supporter.homework.path_runner import _after_delivery

_MENTOR_MARKER = "MARKER-slova-mentora-7731"


class _M(BaseModel):
    n: int


def _validation_error_with_marker() -> ValidationError:
    try:
        _M.model_validate({"n": _MENTOR_MARKER})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")  # pragma: no cover


class TestAfterDelivery:
    async def test_validation_error_logs_without_values_or_exc_info(self) -> None:
        builder = MagicMock()
        builder.after_delivery = AsyncMock(side_effect=_validation_error_with_marker())
        context = MagicMock()

        with capture_logs() as logs:
            await _after_delivery(builder, context, log=structlog.get_logger())

        (entry,) = [
            item for item in logs if item["event"] == "path_after_delivery_failed"
        ]
        assert entry["error"] == "ValidationError"
        assert entry["problems"] == [{"loc": "n", "type": "int_parsing"}]
        assert "exc_info" not in entry
        assert _MENTOR_MARKER not in repr(logs)

    async def test_runtime_error_keeps_exc_info(self) -> None:
        builder = MagicMock()
        builder.after_delivery = AsyncMock(side_effect=RuntimeError("boom"))
        context = MagicMock()

        with capture_logs() as logs:
            await _after_delivery(builder, context, log=structlog.get_logger())

        (entry,) = [
            item for item in logs if item["event"] == "path_after_delivery_failed"
        ]
        assert entry.get("exc_info") is True
