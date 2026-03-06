# S3-021: Full File Upload System

**Phase:** 11 (File Upload)
**Складність:** L
**Статус:** IN PROGRESS
**Залежність:** S3-013 (Course removed, new URL patterns)

## Контекст

Повна специфікація в `current-doc/sprint2-docs/epic-7/S2-058/ISSUES.md` → Q-003.

Зараз файли завантажуються через API сервер (multipart form) — це навантажує RAM/CPU та не масштабується для великих файлів. S3 key pattern `{node_id}/{uuid}/{filename}` не має tenant isolation.

## Два шляхи завантаження

1. **URL** — зовнішнє посилання (YouTube, web page, hosted file) — вже працює
2. **File upload** — локальний файл → presigned URL → S3 → MaterialEntry — **цей таск**

## Блоки роботи

### Блок 1. S3Client — нові методи (`storage/s3.py`)

| Метод | Призначення |
|-------|-------------|
| `generate_presigned_url(key, content_type, expires_in)` | Presigned PUT URL для прямого upload клієнтом |
| `list_objects(prefix)` | Список файлів за prefix (для tenant listing) |
| `get_usage(prefix)` | Сумарний розмір файлів у байтах |
| `delete_object(key)` | Видалення одного файлу з bucket |

### Блок 2. Presigned URL Upload Flow (`routes/materials.py`)

```
Client → POST /nodes/{id}/materials/upload-url (filename, content_type, size)
       ← 200 {upload_url, key, expires_in}
Client → PUT upload_url (file content, direct to S3)
Client → POST /nodes/{id}/materials/confirm-upload (key, source_type)
       ← 201 MaterialEntry
```

**Переваги:** великі файли не проходять через API server, менше RAM, швидше.
Працює з browser upload та backend upload.

**Schemas:**
- `PresignedUrlRequest` — filename, content_type, size_bytes
- `PresignedUrlResponse` — upload_url, key, expires_in
- `ConfirmUploadRequest` — key, source_type, filename

### Блок 3. Storage Management (`routes/storage.py` — НОВИЙ)

| Endpoint | Опис |
|----------|------|
| `GET /storage/files` | Файли тенанта в S3 |
| `GET /storage/usage` | Зайнятий об'єм у байтах |
| `DELETE /storage/files/{key}` | Видалення з S3 + каскад на MaterialEntry |

**Schemas:**
- `StorageFileResponse` — key, size_bytes, last_modified
- `StorageUsageResponse` — total_bytes, file_count

### Блок 4. S3 Cleanup при видаленні сутностей

Синхронний cleanup S3 файлів при видаленні через існуючі endpoints:

| Endpoint | Зміна |
|----------|-------|
| `DELETE /materials/{entry_id}` | Якщо `source_url` вказує на S3 → `s3.delete_object()` |
| `DELETE /nodes/{node_id}` | Зібрати S3 keys з усіх MaterialEntry вузла та дітей → batch delete |

Без цього DB cascade працює, але файли в S3 стають сиротами.

### Multi-tenant Isolation

S3 key prefix: `tenants/{tenant_id}/nodes/{node_id}/{uuid}/{filename}`

- Presigned URLs scoped до конкретного key
- Bucket: private (без підпису доступу немає)
- API валідує tenant ownership перед генерацією URL

### Робота людини (не код)

- **B2 CORS** — налаштувати CORS на bucket для browser upload:
  - `allowedOrigins`: домен(и) клієнта
  - `allowedOperations`: `s3_put`
  - `allowedHeaders`: `content-type`
- Задокументувати в deployment docs (S3-017/S3-018)

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `src/course_supporter/storage/s3.py` | 4 нових методи |
| `src/course_supporter/api/routes/materials.py` | Upload URL + confirm endpoints + S3 cleanup |
| `src/course_supporter/api/routes/storage.py` | **НОВИЙ** — storage management endpoints |
| `src/course_supporter/api/routes/nodes.py` | S3 cleanup при видаленні вузлів |
| `src/course_supporter/api/schemas.py` | Upload/storage schemas |
| `src/course_supporter/api/app.py` | Mount storage router |
| `tests/` | Тести для всіх нових endpoints та S3 методів |

## Acceptance Criteria

- [ ] Presigned URL generation працює з B2/MinIO
- [ ] Direct upload flow: request URL → upload → confirm
- [ ] Storage file listing для tenant
- [ ] Usage tracking (total bytes)
- [ ] File deletion з каскадом на MaterialEntry
- [ ] S3 cleanup при видаленні MaterialEntry та MaterialNode
- [ ] Tenant isolation через S3 prefix
- [ ] CORS задокументовано для B2 deployment
- [ ] Тести покривають всі endpoints та S3 методи
