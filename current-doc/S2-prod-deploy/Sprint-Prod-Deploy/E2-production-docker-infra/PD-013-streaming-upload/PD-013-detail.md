# PD-013: Streaming Upload (1GB) — Detail

## Контекст

Поточний upload endpoint використовує `await file.read()` — зчитує весь файл в RAM. Для 1GB відео це неприйнятно на VPS з 2-4 GB RAM.

## Архітектура

```
Client ──1GB──→ nginx (proxy_request_buffering off)
                  → FastAPI (async read 10MB chunks)
                    → S3 multipart upload (10MB parts)
```

Пікова RAM per upload: ~10-20 MB (один chunk в пам'яті).

## Реалізація

### S3Client — новий метод

```python
# src/course_supporter/storage/s3.py — доповнення

MULTIPART_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB
MULTIPART_THRESHOLD = 50 * 1024 * 1024   # 50 MB — use multipart above this

async def upload_stream(
    self,
    file: AsyncIterator[bytes],
    key: str,
    content_type: str,
    *,
    chunk_size: int = MULTIPART_CHUNK_SIZE,
) -> int:
    """Stream upload to S3 via multipart upload.

    Args:
        file: Async iterator yielding bytes chunks.
        key: S3 object key.
        content_type: MIME type.
        chunk_size: Size of each multipart part.

    Returns:
        Total bytes uploaded.
    """
    # Start multipart upload
    response = await self._client.create_multipart_upload(
        Bucket=self._bucket,
        Key=key,
        ContentType=content_type,
    )
    upload_id = response["UploadId"]

    parts: list[dict] = []
    part_number = 1
    total_bytes = 0
    buffer = bytearray()

    try:
        async for data in file:
            buffer.extend(data)
            total_bytes += len(data)

            while len(buffer) >= chunk_size:
                chunk = bytes(buffer[:chunk_size])
                buffer = bytearray(buffer[chunk_size:])

                part_response = await self._client.upload_part(
                    Bucket=self._bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=chunk,
                )
                parts.append({
                    "ETag": part_response["ETag"],
                    "PartNumber": part_number,
                })
                part_number += 1

        # Upload remaining buffer
        if buffer:
            part_response = await self._client.upload_part(
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=part_number,
                Body=bytes(buffer),
            )
            parts.append({
                "ETag": part_response["ETag"],
                "PartNumber": part_number,
            })

        # Complete multipart upload
        await self._client.complete_multipart_upload(
            Bucket=self._bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )

        return total_bytes

    except Exception:
        # Abort on failure — clean up partial upload
        await self._client.abort_multipart_upload(
            Bucket=self._bucket,
            Key=key,
            UploadId=upload_id,
        )
        raise
```

### Upload helper для FastAPI UploadFile

```python
# src/course_supporter/storage/s3.py

async def _upload_file_chunks(
    file: UploadFile,
    chunk_size: int = MULTIPART_CHUNK_SIZE,
) -> AsyncIterator[bytes]:
    """Async generator that yields chunks from UploadFile."""
    while True:
        data = await file.read(chunk_size)
        if not data:
            break
        yield data
```

### Upload endpoint — зміни

```python
# src/course_supporter/api/routes/courses.py — модифікація materials endpoint

async def upload_material(...):
    ...
    file_size = await s3.upload_stream(
        file=_upload_file_chunks(file),
        key=s3_key,
        content_type=file.content_type or "application/octet-stream",
    )
    ...
```

### Вибір між простим та multipart upload

Для малих файлів (< 50 MB) multipart overhead зайвий. Оригінальний `upload()` залишається для малих файлів:

```python
async def upload_smart(
    self,
    file: UploadFile,
    key: str,
    content_type: str,
) -> int:
    """Choose upload strategy based on content-length hint."""
    # If content_length available and small — simple upload
    if file.size and file.size < MULTIPART_THRESHOLD:
        data = await file.read()
        await self.upload(data, key, content_type)
        return len(data)

    # Otherwise — streaming multipart
    return await self.upload_stream(
        file=_upload_file_chunks(file),
        key=key,
        content_type=content_type,
    )
```

## Backblaze B2 Multipart

B2 підтримує S3 multipart upload API. Мінімальний розмір part = 5 MB (ми використовуємо 10 MB). Максимум 10,000 parts = 100 GB максимальний файл.

## Тести

Файл: `tests/unit/test_streaming_upload.py`

1. **test_upload_stream_small_file** — файл < chunk size → один part
2. **test_upload_stream_multi_part** — файл = 3 chunks → 3 parts + complete
3. **test_upload_stream_abort_on_error** — помилка → abort multipart
4. **test_upload_smart_small_uses_simple** — < threshold → простий upload
5. **test_upload_smart_large_uses_multipart** — > threshold → multipart
6. **test_upload_file_chunks_generator** — правильно yield-ить chunks

Очікувана кількість тестів: **6**

## Definition of Done

- [ ] `S3Client.upload_stream()` з multipart
- [ ] `upload_smart()` вибирає стратегію
- [ ] Abort on failure (cleanup)
- [ ] Upload endpoint використовує streaming
- [ ] 6 тестів зелені
- [ ] `make check` зелений
- [ ] Документ оновлений відповідно до фінальної реалізації
