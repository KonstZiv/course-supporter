"""Async S3 client for MinIO/S3 object storage."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any

import structlog
from aiobotocore.session import get_session as get_aio_session
from botocore.exceptions import ClientError
from fastapi import UploadFile

logger = structlog.get_logger()

MULTIPART_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB
MULTIPART_THRESHOLD = 50 * 1024 * 1024  # 50 MB


class S3Client:
    """Async S3 client wrapping aiobotocore.

    Usage::

        async with S3Client(endpoint, access_key, secret_key, bucket) as s3:
            url = await s3.upload_file("key.pdf", data, "application/pdf")
    """

    def __init__(
        self,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
    ) -> None:
        self._endpoint_url = endpoint_url.rstrip("/")
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._session = get_aio_session()
        self._client_ctx: Any = None
        self._client: Any = None

    async def __aenter__(self) -> S3Client:
        """Create and enter the S3 client context."""
        self._client_ctx = self._session.create_client(
            "s3",
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
        )
        self._client = await self._client_ctx.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close the S3 client context."""
        if self._client_ctx is not None:
            await self._client_ctx.__aexit__(exc_type, exc_val, exc_tb)
            self._client_ctx = None
            self._client = None

    async def upload_file(
        self,
        key: str,
        data: bytes,
        content_type: str,
    ) -> str:
        """Upload file to S3 bucket.

        Args:
            key: Object key (path) in the bucket.
            data: File content as bytes.
            content_type: MIME type of the file.

        Returns:
            The S3 object URL.
        """
        if self._client is None:
            msg = "S3Client not initialized. Use 'async with S3Client(...)'"
            raise RuntimeError(msg)

        await self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        url = f"{self._endpoint_url}/{self._bucket}/{key}"
        logger.info("s3_upload", key=key, content_type=content_type)
        return url

    async def upload_stream(
        self,
        stream: AsyncIterator[bytes],
        key: str,
        content_type: str,
        *,
        chunk_size: int = MULTIPART_CHUNK_SIZE,
    ) -> int:
        """Stream upload to S3 via multipart upload.

        Args:
            stream: Async iterator yielding bytes chunks.
            key: S3 object key.
            content_type: MIME type.
            chunk_size: Size of each multipart part.

        Returns:
            Total bytes uploaded.
        """
        if self._client is None:
            msg = "S3Client not initialized. Use 'async with S3Client(...)'"
            raise RuntimeError(msg)

        response = await self._client.create_multipart_upload(
            Bucket=self._bucket,
            Key=key,
            ContentType=content_type,
        )
        upload_id: str = response["UploadId"]

        parts: list[dict[str, object]] = []
        part_number = 1
        total_bytes = 0
        buffer = bytearray()

        try:
            async for data in stream:
                buffer.extend(data)
                total_bytes += len(data)

                while len(buffer) >= chunk_size:
                    chunk = bytes(buffer[:chunk_size])
                    buffer = bytearray(buffer[chunk_size:])

                    part_resp = await self._client.upload_part(
                        Bucket=self._bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=part_number,
                        Body=chunk,
                    )
                    parts.append(
                        {
                            "ETag": part_resp["ETag"],
                            "PartNumber": part_number,
                        }
                    )
                    part_number += 1

            # Upload remaining buffer
            if buffer:
                part_resp = await self._client.upload_part(
                    Bucket=self._bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=bytes(buffer),
                )
                parts.append(
                    {
                        "ETag": part_resp["ETag"],
                        "PartNumber": part_number,
                    }
                )

            await self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )

            logger.info(
                "s3_multipart_upload",
                key=key,
                total_bytes=total_bytes,
                parts=len(parts),
            )
            return total_bytes

        except Exception:
            await self._client.abort_multipart_upload(
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
            )
            raise

    async def upload_smart(
        self,
        stream: AsyncIterator[bytes],
        key: str,
        content_type: str,
        *,
        file_size: int | None = None,
    ) -> tuple[str, int]:
        """Choose upload strategy based on file size hint.

        Uses simple put_object for small files (< 50 MB),
        multipart streaming for large files.

        Args:
            stream: Async iterator yielding bytes chunks.
            key: S3 object key.
            content_type: MIME type.
            file_size: Optional file size hint.

        Returns:
            Tuple of (S3 URL, total bytes uploaded).
        """
        if file_size is not None and file_size < MULTIPART_THRESHOLD:
            # Small file — collect and use simple upload
            chunks: list[bytes] = []
            async for chunk in stream:
                chunks.append(chunk)
            data = b"".join(chunks)
            url = await self.upload_file(key, data, content_type)
            return url, len(data)

        # Large or unknown size — streaming multipart
        total = await self.upload_stream(stream, key, content_type)
        url = f"{self._endpoint_url}/{self._bucket}/{key}"
        return url, total

    async def check_connectivity(self) -> None:
        """Verify S3 bucket is accessible."""
        if self._client is None:
            msg = "S3Client not initialized. Use 'async with S3Client(...)'"
            raise RuntimeError(msg)

        await self._client.head_bucket(Bucket=self._bucket)

    async def ensure_bucket(self) -> None:
        """Verify that the bucket exists.

        On B2/production the bucket must be pre-created via provider console.
        On MinIO/dev the bucket is created by minio-init container.
        """
        if self._client is None:
            msg = "S3Client not initialized. Use 'async with S3Client(...)'"
            raise RuntimeError(msg)

        try:
            await self._client.head_bucket(Bucket=self._bucket)
            logger.info("s3_bucket_verified", bucket=self._bucket)
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchBucket"):
                logger.error(
                    "s3_bucket_not_found",
                    bucket=self._bucket,
                    hint="Create in B2 console or check minio-init in dev",
                )
            raise


async def upload_file_chunks(
    file: UploadFile,
    chunk_size: int = MULTIPART_CHUNK_SIZE,
) -> AsyncIterator[bytes]:
    """Async generator yielding chunks from a FastAPI UploadFile."""
    while True:
        data = await file.read(chunk_size)
        if not data:
            break
        yield data
