"""Tests for S3Client."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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
    async def test_ensure_bucket_raises_if_missing(self) -> None:
        """ensure_bucket() raises ClientError when bucket not found."""
        from unittest.mock import AsyncMock

        from botocore.exceptions import ClientError

        client = S3Client(
            endpoint_url="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="my-bucket",
        )
        client._client = AsyncMock()
        client._client.head_bucket.side_effect = ClientError(
            error_response={"Error": {"Code": "404", "Message": "Not Found"}},
            operation_name="HeadBucket",
        )

        with pytest.raises(ClientError):
            await client.ensure_bucket()

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


class TestExtractKey:
    """Tests for S3Client.extract_key()."""

    def test_extract_key_from_s3_url(self) -> None:
        """extract_key() returns the object key for matching S3 URLs."""
        client = S3Client(
            endpoint_url="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="course-materials",
        )
        url = "http://localhost:9000/course-materials/courses/abc/file.pdf"
        assert client.extract_key(url) == "courses/abc/file.pdf"

    def test_extract_key_returns_none_for_non_s3(self) -> None:
        """extract_key() returns None for external URLs."""
        client = S3Client(
            endpoint_url="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="course-materials",
        )
        assert client.extract_key("https://example.com/file.pdf") is None

    def test_extract_key_returns_none_for_different_bucket(self) -> None:
        """extract_key() returns None when bucket doesn't match."""
        client = S3Client(
            endpoint_url="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="course-materials",
        )
        url = "http://localhost:9000/other-bucket/file.pdf"
        assert client.extract_key(url) is None

    def test_extract_key_virtual_host_style(self) -> None:
        """extract_key() handles virtual-host style URLs (B2/AWS)."""
        client = S3Client(
            endpoint_url="https://s3.us-west-004.backblazeb2.com",
            access_key="key",
            secret_key="secret",
            bucket="course-materials",
        )
        url = "https://course-materials.s3.us-west-004.backblazeb2.com/courses/file.pdf"
        assert client.extract_key(url) == "courses/file.pdf"

    def test_extract_key_virtual_host_no_key(self) -> None:
        """extract_key() returns None for virtual-host URL without key."""
        client = S3Client(
            endpoint_url="https://s3.us-west-004.backblazeb2.com",
            access_key="key",
            secret_key="secret",
            bucket="course-materials",
        )
        url = "https://course-materials.s3.us-west-004.backblazeb2.com/"
        assert client.extract_key(url) is None


class TestDownloadFile:
    """Tests for S3Client.download_file()."""

    @staticmethod
    def _mock_stream(data: bytes) -> MagicMock:
        """Create a mock S3 Body stream returning data then empty bytes."""
        stream = AsyncMock()
        stream.read = AsyncMock(side_effect=[data, b""])
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=stream)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    @pytest.mark.asyncio
    async def test_download_file_writes_content(self, tmp_path: Path) -> None:
        """download_file() streams S3 object to a local file."""
        client = S3Client(
            endpoint_url="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="my-bucket",
        )
        client._client = AsyncMock()
        client._client.get_object = AsyncMock(
            return_value={"Body": self._mock_stream(b"hello world")}
        )

        dest = tmp_path / "out.txt"
        result = await client.download_file("docs/out.txt", dest=dest)

        assert result == dest
        assert dest.read_bytes() == b"hello world"
        client._client.get_object.assert_awaited_once_with(
            Bucket="my-bucket", Key="docs/out.txt"
        )

    @pytest.mark.asyncio
    async def test_download_file_creates_temp_with_suffix(self) -> None:
        """download_file() creates a temp file with the key's suffix."""
        client = S3Client(
            endpoint_url="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="my-bucket",
        )
        client._client = AsyncMock()
        client._client.get_object = AsyncMock(
            return_value={"Body": self._mock_stream(b"pdf-data")}
        )

        result = await client.download_file("courses/lecture.pdf")
        try:
            assert result.suffix == ".pdf"
            assert result.read_bytes() == b"pdf-data"
        finally:
            result.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_download_file_writes_to_explicit_dest(self, tmp_path: Path) -> None:
        """download_file() uses provided dest path."""
        client = S3Client(
            endpoint_url="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="b",
        )
        client._client = AsyncMock()
        client._client.get_object = AsyncMock(
            return_value={"Body": self._mock_stream(b"abc")}
        )

        dest = tmp_path / "explicit.md"
        result = await client.download_file("k.md", dest=dest)
        assert result == dest
        assert dest.read_bytes() == b"abc"

    @pytest.mark.asyncio
    async def test_download_file_raises_without_init(self) -> None:
        """download_file() raises RuntimeError if not initialized."""
        client = S3Client(
            endpoint_url="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="bucket",
        )
        with pytest.raises(RuntimeError, match="not initialized"):
            await client.download_file("some/key.txt")
