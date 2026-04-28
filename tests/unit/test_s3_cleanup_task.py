"""Unit tests for s3_cleanup_task (vision §3 KD13).

Covers the three-tier error policy from the worker module:

* ``ClientError`` with ``NoSuchKey`` / ``404`` / ``NotFound`` →
  counted as deleted (idempotent).
* Other ``ClientError`` (e.g. ``AccessDenied``, throttling
  ``SlowDown``) → recorded per-key in ``errors[]``; task continues.
* Non-``ClientError`` (e.g. ``EndpointConnectionError``) → propagates
  so ARQ's existing retry policy fires.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from course_supporter.workers.s3_cleanup import s3_cleanup_task


def _make_client_error(code: str) -> ClientError:
    """Build a ClientError carrying the given AWS error code."""
    return ClientError(
        error_response={"Error": {"Code": code, "Message": "mocked"}},
        operation_name="DeleteObject",
    )


def _make_ctx(s3_mock: AsyncMock) -> dict[str, Any]:
    return {"s3_client": s3_mock}


class TestEmpty:
    async def test_empty_file_keys_returns_noop(self) -> None:
        """Empty input short-circuits without calling S3 at all."""
        s3 = AsyncMock()
        s3.delete_object = AsyncMock()
        result = await s3_cleanup_task(
            _make_ctx(s3), file_keys=[], job_id=str(uuid.uuid4())
        )
        assert result == {"deleted": [], "errors": []}
        s3.delete_object.assert_not_called()


class TestHappyPath:
    async def test_all_keys_deleted_in_order(self) -> None:
        s3 = AsyncMock()
        s3.delete_object = AsyncMock(return_value=None)
        keys = ["a", "b", "c"]
        result = await s3_cleanup_task(
            _make_ctx(s3), file_keys=keys, job_id=str(uuid.uuid4())
        )
        assert result["deleted"] == keys
        assert result["errors"] == []
        assert s3.delete_object.call_count == 3


class TestIdempotency:
    """Missing S3 object is success per KD13 (cross-provider variants)."""

    async def test_no_such_key_counted_as_deleted(self) -> None:
        s3 = AsyncMock()
        s3.delete_object = AsyncMock(side_effect=_make_client_error("NoSuchKey"))
        result = await s3_cleanup_task(
            _make_ctx(s3), file_keys=["missing"], job_id=str(uuid.uuid4())
        )
        assert result["deleted"] == ["missing"]
        assert result["errors"] == []

    async def test_404_code_counted_as_deleted(self) -> None:
        """B2 / S3-compatible providers may return code='404' instead."""
        s3 = AsyncMock()
        s3.delete_object = AsyncMock(side_effect=_make_client_error("404"))
        result = await s3_cleanup_task(
            _make_ctx(s3), file_keys=["x"], job_id=str(uuid.uuid4())
        )
        assert result["deleted"] == ["x"]
        assert result["errors"] == []

    async def test_not_found_code_counted_as_deleted(self) -> None:
        s3 = AsyncMock()
        s3.delete_object = AsyncMock(side_effect=_make_client_error("NotFound"))
        result = await s3_cleanup_task(
            _make_ctx(s3), file_keys=["x"], job_id=str(uuid.uuid4())
        )
        assert result["deleted"] == ["x"]
        assert result["errors"] == []


class TestPerKeyClientError:
    """Other ClientError codes recorded per-key; task does not abort."""

    async def test_access_denied_in_errors_others_continue(self) -> None:
        """Mid-batch ClientError doesn't abort — surrounding keys still process."""
        s3 = AsyncMock()

        async def side_effect(key: str) -> None:
            if key == "denied":
                raise _make_client_error("AccessDenied")

        s3.delete_object = AsyncMock(side_effect=side_effect)

        result = await s3_cleanup_task(
            _make_ctx(s3),
            file_keys=["ok1", "denied", "ok2"],
            job_id=str(uuid.uuid4()),
        )
        assert result["deleted"] == ["ok1", "ok2"]
        assert len(result["errors"]) == 1
        assert result["errors"][0]["key"] == "denied"
        assert "AccessDenied" in result["errors"][0]["error"]

    async def test_throttling_slowdown_recorded_as_error(self) -> None:
        """Throttling-coded ClientError lands in errors[] (ARQ retry triggers
        only on non-ClientError; throttling without retry-friendly resolution
        becomes operator-visible via Job.result_data)."""
        s3 = AsyncMock()
        s3.delete_object = AsyncMock(side_effect=_make_client_error("SlowDown"))
        result = await s3_cleanup_task(
            _make_ctx(s3), file_keys=["t"], job_id=str(uuid.uuid4())
        )
        assert result["deleted"] == []
        assert len(result["errors"]) == 1
        assert "SlowDown" in result["errors"][0]["error"]


class TestPropagation:
    """Non-ClientError exceptions propagate so ARQ's retry policy fires."""

    async def test_endpoint_connection_error_propagates(self) -> None:
        s3 = AsyncMock()
        s3.delete_object = AsyncMock(
            side_effect=EndpointConnectionError(endpoint_url="http://example.com")
        )
        with pytest.raises(EndpointConnectionError):
            await s3_cleanup_task(
                _make_ctx(s3), file_keys=["x"], job_id=str(uuid.uuid4())
            )
