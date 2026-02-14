"""Tests for S3Client."""

import pytest

from course_supporter.storage.s3 import S3Client


class TestS3Client:
    def test_init_stores_config(self) -> None:
        """S3Client stores configuration parameters."""
        client = S3Client(
            endpoint_url="http://localhost:9000",
            access_key="test-key",
            secret_key="test-secret",
            bucket="test-bucket",
        )
        assert client._endpoint_url == "http://localhost:9000"
        assert client._bucket == "test-bucket"

    @pytest.mark.asyncio
    async def test_upload_file_raises_without_init(self) -> None:
        """upload_file() raises RuntimeError if not initialized."""
        client = S3Client(
            endpoint_url="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="bucket",
        )
        with pytest.raises(RuntimeError, match="not initialized"):
            await client.upload_file("key", b"data", "text/plain")

    @pytest.mark.asyncio
    async def test_ensure_bucket_raises_without_init(self) -> None:
        """ensure_bucket() raises RuntimeError if not initialized."""
        client = S3Client(
            endpoint_url="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="bucket",
        )
        with pytest.raises(RuntimeError, match="not initialized"):
            await client.ensure_bucket()

    @pytest.mark.asyncio
    async def test_upload_file_returns_url(self) -> None:
        """upload_file() calls put_object and returns URL."""
        from unittest.mock import AsyncMock

        client = S3Client(
            endpoint_url="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="my-bucket",
        )
        client._client = AsyncMock()

        url = await client.upload_file("path/file.pdf", b"content", "application/pdf")

        assert url == "http://localhost:9000/my-bucket/path/file.pdf"
        client._client.put_object.assert_awaited_once_with(
            Bucket="my-bucket",
            Key="path/file.pdf",
            Body=b"content",
            ContentType="application/pdf",
        )

    @pytest.mark.asyncio
    async def test_ensure_bucket_creates_if_missing(self) -> None:
        """ensure_bucket() creates bucket when head_bucket fails."""
        from unittest.mock import AsyncMock

        client = S3Client(
            endpoint_url="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="my-bucket",
        )
        client._client = AsyncMock()
        client._client.head_bucket.side_effect = Exception("Not found")

        await client.ensure_bucket()

        client._client.create_bucket.assert_awaited_once_with(Bucket="my-bucket")

    @pytest.mark.asyncio
    async def test_ensure_bucket_skips_if_exists(self) -> None:
        """ensure_bucket() does nothing when bucket exists."""
        from unittest.mock import AsyncMock

        client = S3Client(
            endpoint_url="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="my-bucket",
        )
        client._client = AsyncMock()

        await client.ensure_bucket()

        client._client.head_bucket.assert_awaited_once()
        client._client.create_bucket.assert_not_called()
