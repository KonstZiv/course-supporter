"""Tests for S3Client: presigned URL, head, delete, list, usage."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.storage.s3 import S3Client


def _make_client() -> S3Client:
    client = S3Client(
        endpoint_url="http://localhost:9000",
        access_key="key",
        secret_key="secret",
        bucket="test-bucket",
    )
    client._client = AsyncMock()
    return client


class TestGeneratePresignedUrl:
    async def test_returns_presigned_url(self) -> None:
        """generate_presigned_url() returns URL from S3 client."""
        client = _make_client()
        client._client.generate_presigned_url = AsyncMock(
            return_value="https://s3.example.com/test-bucket/key?sig=abc"
        )

        url = await client.generate_presigned_url(
            "tenants/t1/file.pdf", "application/pdf"
        )

        assert url == "https://s3.example.com/test-bucket/key?sig=abc"
        client._client.generate_presigned_url.assert_awaited_once_with(
            "put_object",
            Params={
                "Bucket": "test-bucket",
                "Key": "tenants/t1/file.pdf",
                "ContentType": "application/pdf",
            },
            ExpiresIn=3600,
        )

    async def test_custom_expiry(self) -> None:
        """Custom expires_in is forwarded to S3."""
        client = _make_client()
        client._client.generate_presigned_url = AsyncMock(return_value="url")

        await client.generate_presigned_url("key", "text/plain", expires_in=900)

        call_kwargs = client._client.generate_presigned_url.call_args
        assert call_kwargs.kwargs["ExpiresIn"] == 900

    async def test_raises_without_init(self) -> None:
        """Raises RuntimeError if client not initialized."""
        client = S3Client("http://x", "a", "b", "c")
        with pytest.raises(RuntimeError, match="not initialized"):
            await client.generate_presigned_url("k", "text/plain")


class TestGeneratePresignedGetUrl:
    async def test_signs_get_object_with_flat_ttl(self) -> None:
        """generate_presigned_get_url() signs get_object at the flat KD17 TTL."""
        from course_supporter.storage.s3 import PRESIGNED_GET_TTL_SEC

        client = _make_client()
        client._client.generate_presigned_url = AsyncMock(
            return_value="https://s3.example.com/test-bucket/slide?sig=xyz"
        )

        url = await client.generate_presigned_get_url(
            "tenants/t1/nodes/n1/slides/d1/0001.webp"
        )

        assert url == "https://s3.example.com/test-bucket/slide?sig=xyz"
        assert PRESIGNED_GET_TTL_SEC == 18000
        # No ContentType in the signed params (unlike the PUT variant).
        client._client.generate_presigned_url.assert_awaited_once_with(
            "get_object",
            Params={
                "Bucket": "test-bucket",
                "Key": "tenants/t1/nodes/n1/slides/d1/0001.webp",
            },
            ExpiresIn=PRESIGNED_GET_TTL_SEC,
        )

    async def test_custom_expiry(self) -> None:
        """Custom expires_in is forwarded to S3."""
        client = _make_client()
        client._client.generate_presigned_url = AsyncMock(return_value="url")

        await client.generate_presigned_get_url("key", expires_in=120)

        call_kwargs = client._client.generate_presigned_url.call_args
        assert call_kwargs.kwargs["ExpiresIn"] == 120

    async def test_content_disposition_signed_when_set(self) -> None:
        """content_disposition signs ResponseContentDisposition (R3 code path)."""
        client = _make_client()
        client._client.generate_presigned_url = AsyncMock(return_value="url")

        await client.generate_presigned_get_url(
            "key",
            content_disposition='attachment; filename="script.py"',
        )

        params = client._client.generate_presigned_url.call_args.kwargs["Params"]
        assert params["ResponseContentDisposition"] == (
            'attachment; filename="script.py"'
        )

    async def test_response_content_type_signed_when_set(self) -> None:
        """response_content_type signs ResponseContentType (text charset path).

        boto serialises this Param into the ``response-content-type`` query of
        the presigned URL, so S3/B2 serves the object with that Content-Type.
        """
        client = _make_client()
        client._client.generate_presigned_url = AsyncMock(return_value="url")

        await client.generate_presigned_get_url(
            "key",
            response_content_type="text/plain; charset=utf-8",
        )

        params = client._client.generate_presigned_url.call_args.kwargs["Params"]
        assert params["ResponseContentType"] == "text/plain; charset=utf-8"

    async def test_response_content_type_absent_when_none(self) -> None:
        """No response_content_type (default) → ResponseContentType not signed."""
        client = _make_client()
        client._client.generate_presigned_url = AsyncMock(return_value="url")

        await client.generate_presigned_get_url("key")

        params = client._client.generate_presigned_url.call_args.kwargs["Params"]
        assert "ResponseContentType" not in params

    async def test_raises_without_init(self) -> None:
        """Raises RuntimeError if client not initialized."""
        client = S3Client("http://x", "a", "b", "c")
        with pytest.raises(RuntimeError, match="not initialized"):
            await client.generate_presigned_get_url("k")


class TestHeadObject:
    async def test_returns_metadata(self) -> None:
        """head_object() returns metadata dict."""
        client = _make_client()
        meta = {"ContentLength": 1024, "ContentType": "application/pdf"}
        client._client.head_object = AsyncMock(return_value=meta)

        result = await client.head_object("tenants/t1/file.pdf")

        assert result["ContentLength"] == 1024
        client._client.head_object.assert_awaited_once_with(
            Bucket="test-bucket", Key="tenants/t1/file.pdf"
        )

    async def test_raises_without_init(self) -> None:
        """Raises RuntimeError if client not initialized."""
        client = S3Client("http://x", "a", "b", "c")
        with pytest.raises(RuntimeError, match="not initialized"):
            await client.head_object("k")


class TestDeleteObject:
    async def test_calls_delete(self) -> None:
        """delete_object() calls S3 delete_object."""
        client = _make_client()

        await client.delete_object("tenants/t1/file.pdf")

        client._client.delete_object.assert_awaited_once_with(
            Bucket="test-bucket", Key="tenants/t1/file.pdf"
        )

    async def test_raises_without_init(self) -> None:
        """Raises RuntimeError if client not initialized."""
        client = S3Client("http://x", "a", "b", "c")
        with pytest.raises(RuntimeError, match="not initialized"):
            await client.delete_object("k")


class TestListObjects:
    async def test_returns_objects(self) -> None:
        """list_objects() returns list of file metadata dicts."""
        client = _make_client()
        now = datetime.now(UTC)
        page = {
            "Contents": [
                {"Key": "tenants/t1/a.pdf", "Size": 100, "LastModified": now},
                {"Key": "tenants/t1/b.mp4", "Size": 200, "LastModified": now},
            ]
        }
        paginator = MagicMock()
        paginator.paginate.return_value = _async_pages([page])
        client._client.get_paginator = MagicMock(return_value=paginator)

        result = await client.list_objects("tenants/t1/")

        assert len(result) == 2
        assert result[0] == {
            "key": "tenants/t1/a.pdf",
            "size": 100,
            "last_modified": now,
        }
        assert result[1]["key"] == "tenants/t1/b.mp4"

    async def test_empty_prefix(self) -> None:
        """Returns empty list when no objects match."""
        client = _make_client()
        paginator = MagicMock()
        paginator.paginate.return_value = _async_pages([{}])
        client._client.get_paginator = MagicMock(return_value=paginator)

        result = await client.list_objects("tenants/nonexistent/")

        assert result == []

    async def test_multiple_pages(self) -> None:
        """Handles paginated results correctly."""
        client = _make_client()
        now = datetime.now(UTC)
        pages = [
            {"Contents": [{"Key": "a", "Size": 10, "LastModified": now}]},
            {"Contents": [{"Key": "b", "Size": 20, "LastModified": now}]},
        ]
        paginator = MagicMock()
        paginator.paginate.return_value = _async_pages(pages)
        client._client.get_paginator = MagicMock(return_value=paginator)

        result = await client.list_objects("prefix/")

        assert len(result) == 2

    async def test_raises_without_init(self) -> None:
        """Raises RuntimeError if client not initialized."""
        client = S3Client("http://x", "a", "b", "c")
        with pytest.raises(RuntimeError, match="not initialized"):
            await client.list_objects("p/")


class TestGetUsage:
    async def test_sums_sizes(self) -> None:
        """get_usage() returns total bytes and file count."""
        client = _make_client()
        now = datetime.now(UTC)
        page = {
            "Contents": [
                {"Key": "a", "Size": 100, "LastModified": now},
                {"Key": "b", "Size": 250, "LastModified": now},
                {"Key": "c", "Size": 50, "LastModified": now},
            ]
        }
        paginator = MagicMock()
        paginator.paginate.return_value = _async_pages([page])
        client._client.get_paginator = MagicMock(return_value=paginator)

        total_bytes, file_count = await client.get_usage("prefix/")

        assert total_bytes == 400
        assert file_count == 3

    async def test_empty_returns_zero(self) -> None:
        """Empty prefix returns (0, 0)."""
        client = _make_client()
        paginator = MagicMock()
        paginator.paginate.return_value = _async_pages([{}])
        client._client.get_paginator = MagicMock(return_value=paginator)

        total_bytes, file_count = await client.get_usage("empty/")

        assert total_bytes == 0
        assert file_count == 0


# -- Helpers --


async def _async_pages(pages: list[dict]) -> None:  # type: ignore[misc]
    """Async generator yielding pages (simulates paginator)."""
    for page in pages:
        yield page  # type: ignore[misc]
