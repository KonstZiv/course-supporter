"""Async S3 client for MinIO/S3 object storage."""

from __future__ import annotations

from types import TracebackType
from typing import Any

import structlog
from aiobotocore.session import get_session as get_aio_session

logger = structlog.get_logger()


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
        self._endpoint_url = endpoint_url
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
        except Exception:
            logger.error(
                "s3_bucket_not_found",
                bucket=self._bucket,
                hint="Create bucket manually in provider console (B2/MinIO)",
            )
            raise
