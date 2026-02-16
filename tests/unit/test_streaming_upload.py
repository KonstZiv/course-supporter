"""Tests for S3 streaming upload (multipart)."""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from course_supporter.storage.s3 import (
    MULTIPART_CHUNK_SIZE,
    MULTIPART_THRESHOLD,
    S3Client,
    upload_file_chunks,
)


def _make_client() -> S3Client:
    client = S3Client(
        endpoint_url="http://localhost:9000",
        access_key="key",
        secret_key="secret",
        bucket="my-bucket",
    )
    client._client = AsyncMock()
    client._client.create_multipart_upload.return_value = {"UploadId": "test-upload-id"}
    client._client.upload_part.return_value = {"ETag": "test-etag"}
    return client


async def _async_iter(data: bytes, chunk_size: int) -> AsyncIterator[bytes]:
    """Yield data in chunks."""
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]


class TestUploadStream:
    @pytest.mark.asyncio
    async def test_small_file_single_part(self) -> None:
        """File smaller than chunk size produces one part."""
        client = _make_client()
        data = b"x" * 100

        total = await client.upload_stream(
            stream=_async_iter(data, 100),
            key="test/file.bin",
            content_type="application/octet-stream",
        )

        assert total == 100
        client._client.upload_part.assert_awaited_once()
        client._client.complete_multipart_upload.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_multi_part_upload(self) -> None:
        """File spanning multiple chunks produces correct number of parts."""
        client = _make_client()
        chunk = MULTIPART_CHUNK_SIZE
        data = b"x" * (chunk * 3)

        total = await client.upload_stream(
            stream=_async_iter(data, chunk),
            key="test/large.bin",
            content_type="video/mp4",
        )

        assert total == chunk * 3
        assert client._client.upload_part.await_count == 3
        client._client.complete_multipart_upload.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_abort_on_error(self) -> None:
        """Multipart upload is aborted when an error occurs."""
        client = _make_client()
        client._client.upload_part.side_effect = RuntimeError("network error")

        with pytest.raises(RuntimeError, match="network error"):
            await client.upload_stream(
                stream=_async_iter(b"x" * MULTIPART_CHUNK_SIZE, MULTIPART_CHUNK_SIZE),
                key="test/fail.bin",
                content_type="application/octet-stream",
            )

        client._client.abort_multipart_upload.assert_awaited_once_with(
            Bucket="my-bucket",
            Key="test/fail.bin",
            UploadId="test-upload-id",
        )

    @pytest.mark.asyncio
    async def test_raises_without_init(self) -> None:
        """upload_stream() raises RuntimeError if not initialized."""
        client = S3Client(
            endpoint_url="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="bucket",
        )
        with pytest.raises(RuntimeError, match="not initialized"):
            await client.upload_stream(
                stream=_async_iter(b"data", 4),
                key="k",
                content_type="text/plain",
            )


class TestUploadSmart:
    @pytest.mark.asyncio
    async def test_small_uses_simple_upload(self) -> None:
        """Files below threshold use simple put_object."""
        client = _make_client()
        data = b"small file"

        url, total = await client.upload_smart(
            stream=_async_iter(data, len(data)),
            key="test/small.txt",
            content_type="text/plain",
            file_size=len(data),
        )

        assert total == len(data)
        assert "my-bucket/test/small.txt" in url
        client._client.put_object.assert_awaited_once()
        client._client.create_multipart_upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_large_uses_multipart(self) -> None:
        """Files above threshold use multipart upload."""
        client = _make_client()
        size = MULTIPART_THRESHOLD + 1
        data = b"x" * size

        _url, total = await client.upload_smart(
            stream=_async_iter(data, MULTIPART_CHUNK_SIZE),
            key="test/large.bin",
            content_type="video/mp4",
            file_size=size,
        )

        assert total == size
        client._client.create_multipart_upload.assert_awaited_once()
        client._client.put_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_size_uses_multipart(self) -> None:
        """When file_size is None, multipart is used."""
        client = _make_client()
        data = b"x" * 100

        _url, total = await client.upload_smart(
            stream=_async_iter(data, 100),
            key="test/unknown.bin",
            content_type="application/octet-stream",
            file_size=None,
        )

        assert total == 100
        client._client.create_multipart_upload.assert_awaited_once()


class TestUploadFileChunks:
    @pytest.mark.asyncio
    async def test_yields_correct_chunks(self) -> None:
        """upload_file_chunks yields data in chunk_size pieces."""
        mock_file = AsyncMock()
        mock_file.read.side_effect = [b"aaa", b"bb", b""]

        chunks = [c async for c in upload_file_chunks(mock_file, chunk_size=3)]

        assert chunks == [b"aaa", b"bb"]
        assert mock_file.read.await_count == 3
