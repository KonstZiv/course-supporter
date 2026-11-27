"""Unit tests for old-path failure handling in ``arq_process_homework`` (DD-SP-DO)."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel, ValidationError
from structlog.testing import capture_logs

from course_supporter.api.routes._portal_shared import FAILED_REASON_CODE
from course_supporter.api.tasks import arq_process_homework

_MENTOR_MARKER = "MARKER-slova-mentora-7731"
_BODY = arq_process_homework.__wrapped__  # type: ignore[attr-defined]


class _M(BaseModel):
    n: int


def _validation_error_with_marker() -> ValidationError:
    try:
        _M.model_validate({"n": _MENTOR_MARKER})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")  # pragma: no cover


def _session_factory() -> MagicMock:
    session = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=cm)


async def _run_old_path_raising(
    exc: Exception,
) -> tuple[list[dict[str, Any]], AsyncMock, AsyncMock]:
    hw_repo = MagicMock()
    hw_repo.get_by_id = AsyncMock(side_effect=exc)
    hw_repo.update_status = AsyncMock()
    notify = AsyncMock()
    ctx = {
        "session_factory": _session_factory(),
        "stage_router": MagicMock(),
        "s3_client": MagicMock(),
    }

    with (
        patch("course_supporter.api.tasks.set_tenant_from_job", new=AsyncMock()),
        patch("course_supporter.api.tasks.set_job_from_arq"),
        patch(
            "course_supporter.homework.path_runner.run_new_path_if_switched",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "course_supporter.storage.homework_repository.HomeworkRepository",
            return_value=hw_repo,
        ),
        patch("course_supporter.api.tasks._notify_homework_failed", notify),
        capture_logs() as logs,
        pytest.raises(type(exc)),
    ):
        await _BODY(ctx, str(uuid.uuid4()), str(uuid.uuid4()))

    return logs, hw_repo.update_status, notify


class TestOldPathFailurePrivacy:
    async def test_validation_error_keeps_values_out_of_db_webhook_and_logs(
        self,
    ) -> None:
        logs, update_status, notify = await _run_old_path_raising(
            _validation_error_with_marker()
        )

        error_message = update_status.await_args.kwargs["error_message"]
        assert error_message == "ValidationError: n int_parsing"
        assert _MENTOR_MARKER not in error_message

        assert notify.await_args.kwargs["reason"] == FAILED_REASON_CODE
        assert notify.await_args.kwargs["reason"] == "processing_failed"

        (line,) = [
            entry for entry in logs if entry["event"] == "homework_processing_failed"
        ]
        assert line["error"] == "ValidationError"
        assert line["problems"] == [{"loc": "n", "type": "int_parsing"}]
        assert _MENTOR_MARKER not in repr(logs)

    async def test_runtime_error_keeps_message_in_db_and_logs_and_code_in_webhook(
        self,
    ) -> None:
        logs, update_status, notify = await _run_old_path_raising(RuntimeError("boom"))

        assert update_status.await_args.kwargs["error_message"] == "boom"
        assert notify.await_args.kwargs["reason"] == "processing_failed"
        (line,) = [
            entry for entry in logs if entry["event"] == "homework_processing_failed"
        ]
        assert line["error"] == "boom"
