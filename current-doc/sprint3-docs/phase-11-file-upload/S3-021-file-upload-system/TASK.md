# S3-021: Full File Upload System

**Phase:** 11 (File Upload)
**Складність:** L
**Статус:** PENDING
**Залежність:** S3-013 (Course removed, new URL patterns)

## Контекст

Повна специфікація в `current-doc/sprint2-docs/epic-7/S2-058/ISSUES.md` → Q-003.

## Два шляхи завантаження

1. **URL** — зовнішнє посилання (YouTube, web page, hosted file)
2. **File upload** — локальний файл → S3 → MaterialEntry

## Компоненти

### 1. Presigned URL Upload Flow

```
Client → POST /nodes/{id}/materials/upload-url (filename, content_type, size)
       ← 200 {upload_url, key, expires_in}
Client → PUT upload_url (file content, direct to S3)
Client → POST /nodes/{id}/materials/confirm-upload (key, source_type)
       ← 201 MaterialEntry
```

**Переваги:** великі файли не проходять через API server, менше RAM, швидше.

### 2. Storage Management

- `GET /storage/files` — список файлів tenant'а в S3
- `GET /storage/usage` — загальний обсяг зайнятого місця
- `DELETE /storage/files/{key}` — видалити файл (з каскадом на MaterialEntry)

### 3. Multi-tenant Isolation

S3 key prefix: `tenants/{tenant_id}/nodes/{node_id}/{uuid}/{filename}`

Bucket policy не потрібна (presigned URLs вже scoped до конкретного key).

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `src/course_supporter/storage/s3.py` | `generate_presigned_url()`, `list_objects()`, `get_usage()`, `delete_object()` |
| `src/course_supporter/api/routes/materials.py` | Upload URL + confirm endpoints |
| `src/course_supporter/api/routes/storage.py` | НОВИЙ — storage management endpoints |
| `src/course_supporter/api/schemas.py` | Upload/storage schemas |
| `src/course_supporter/api/app.py` | Mount storage router |
| `tests/` | Тести для presigned URLs, storage endpoints |

## Деталі реалізації

### S3Client.generate_presigned_url()

```python
async def generate_presigned_url(
    self, key: str, content_type: str, expires_in: int = 3600
) -> str:
    """Generate presigned PUT URL for direct upload."""
    url = await self._client.generate_presigned_url(
        "put_object",
        Params={"Bucket": self._bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=expires_in,
    )
    return url
```

### S3Client.list_objects()

```python
async def list_objects(self, prefix: str) -> list[dict]:
    """List objects with given prefix."""
    paginator = self._client.get_paginator("list_objects_v2")
    objects = []
    async for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            objects.append({
                "key": obj["Key"],
                "size": obj["Size"],
                "last_modified": obj["LastModified"],
            })
    return objects
```

### Security

- Presigned URLs expiry: 1 hour default
- Tenant isolation через key prefix (API validates tenant before generating URL)
- Private bucket — no public access
- Confirm endpoint validates that file exists in S3 before creating MaterialEntry

## Acceptance Criteria

- [ ] Presigned URL generation працює з B2
- [ ] Direct upload flow: request URL → upload → confirm
- [ ] Storage file listing для tenant
- [ ] Usage tracking (total bytes)
- [ ] File deletion з каскадом
- [ ] Tenant isolation через S3 prefix
- [ ] Тести покривають всі endpoints
