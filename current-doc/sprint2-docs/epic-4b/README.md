# Epic 4b: S3 Download for File-Based Processors

## Problem

When a file is uploaded via `POST /api/v1/courses/{id}/materials`, the API:
1. Uploads the file to MinIO/S3
2. Stores the S3 URL as `source_url` in the DB: `http://localhost:9000/course-materials/{course_id}/{uuid}/filename.md`

Three out of four processors use `Path(source.source_url)` to open the file **locally**, which fails with `FileNotFoundError` because the S3 URL is not a valid local filesystem path.

**Discovered:** Smoke testing after Epic 4 completion. Uploaded `.md` file → job failed:
```
[Errno 2] No such file or directory: 'http:/localhost:9000/course-materials/.../smoke-test.md'
```

Note: `Path()` also normalizes `http://` → `http:/` (strips one slash).

## Affected Processors

| Processor | File:Line | How it uses source_url | Impact |
|-----------|-----------|----------------------|--------|
| **TextProcessor** | `text.py:49` | `Path(source.source_url)` → `read_text()` | **Broken** for uploaded files |
| **PresentationProcessor** | `presentation.py:49` | `Path(source.source_url)` → `fitz.open()` / `pptx.Presentation()` | **Broken** for uploaded files |
| **WhisperVideoProcessor** | `video.py:242` | URL for FFmpeg/yt-dlp | **Partially works** (yt-dlp handles http, but S3 auth may fail) |
| **WebProcessor** | `web.py:54` | URL → `local_scrape_web()` | **Not affected** (always HTTP URL) |

## Current State

- `S3Client` (`storage/s3.py`) has **only upload methods**: `upload_file`, `upload_stream`, `upload_smart`
- **No download capability** exists anywhere in the codebase
- Processors assume `source_url` is always a local path (for file types) or an HTTP URL (for web)

## Solution

Two tasks, executed sequentially:

### S2-037a: S3Client.download_file()

Add a download method to the existing `S3Client`:

```python
async def download_file(self, key: str, dest: Path | None = None) -> Path:
    """Download an S3 object to a local temp file.

    Args:
        key: S3 object key (e.g. "course-id/uuid/file.md").
        dest: Optional destination path. If None, creates a temp file.

    Returns:
        Path to the downloaded local file.
    """
```

- Uses `aiobotocore` `get_object()` (same session as uploads)
- Streams to disk (not full in-memory) for large files
- Returns `Path` to the local file
- Unit tests with mocked aiobotocore

### S2-037b: URL Resolution in tasks.py

Add a resolution step in `arq_ingest_material` and `ingest_material` **before** calling `processor.process()`:

```python
# Pseudocode
temp_path: Path | None = None
try:
    if is_s3_url(source_url):
        key = extract_s3_key(source_url)
        temp_path = await s3_client.download_file(key)
        material.source_url = str(temp_path)  # Replace with local path

    doc = await processor.process(material, router=router)
finally:
    if temp_path and temp_path.exists():
        temp_path.unlink()
```

**Key decisions:**
- Resolution happens in `tasks.py`, NOT inside individual processors
- Processors stay simple — they always receive either local paths or native URLs
- S3 URL detection: check if `source_url` starts with the configured S3 endpoint URL
- Cleanup in `finally` block to avoid temp file leaks
- Need `S3Client` instance in ARQ worker context (add to `startup`)

**Tests:**
- Unit: mock S3Client, verify download + cleanup + error handling
- Integration: real MinIO, upload → download → verify content matches

## Dependencies

- **Epic 4** (completed): processors have DI slots, factory wiring
- **Epic 1** (completed): ARQ worker context, tasks.py structure
- **S3Client** (existing): `storage/s3.py` — extend with download

## Files to Modify/Create

| Action | File |
|--------|------|
| MODIFY | `src/course_supporter/storage/s3.py` — add `download_file()` |
| MODIFY | `src/course_supporter/api/tasks.py` — add URL resolution before processing |
| MODIFY | `src/course_supporter/worker.py` — add S3Client to ARQ startup context |
| CREATE | `tests/unit/test_s3_download.py` — unit tests for download_file |
| MODIFY | `tests/unit/test_api/test_ingestion_task.py` — test URL resolution logic |

## Estimate

- S2-037a: 2h
- S2-037b: 3h
- **Total: ~0.5-1 day**
