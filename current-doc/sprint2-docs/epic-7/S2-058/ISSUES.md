# S2-058: Issues & Questions (Post-Testing)

## Bugs

### BUG-001: GET /nodes/tree returns 500
- **Endpoint:** `GET /api/v1/courses/{course_id}/nodes/tree`
- **Request:** `curl -s .../courses/019cb3ee-32b0-7602-a292-ce156a249e9d/nodes/tree -H "X-API-Key: ..."`
- **Expected:** 200, tree JSON
- **Got:** `{"detail":"Internal server error"}`, HTTP 500
- **Context:** Course has root + 2 children + 8 grandchildren. All nodes created via POST without errors.

### BUG-002: Ingestion task fails — SourceMaterial not found for MaterialEntry
- **Symptom:** All 8 video ingestion jobs stuck in `queued` despite worker running
- **Root cause (1):** `arq_ingest_material` task (tasks.py:133) calls `SourceMaterialRepository.update_status()` which queries `source_materials` table. But materials created via `POST /nodes/{node_id}/materials` are `MaterialEntry` records (different table). Task not adapted for Sprint 2 model.
- **Root cause (2):** Error handler tries transition `queued → failed`, but Job state machine requires `queued → active` first. `on_failure()` in `ingestion_callback.py:118` should first set `active`, then `failed`.
- **Log:** `ValueError: SourceMaterial not found: {id}` → `ValueError: Invalid job status transition: 'queued' → 'failed'`
- **Impact:** BLOCKER — no ingestion from MaterialEntry works at all

## Architecture Questions

### Q-001: Why separate Course entity from root Node?
Чому курс і кореневий вузол — це дві окремі сутності? Чому кореневий вузол не може сам бути курсом? Що дає додаткова абстракція Course, чого не вистачає root node?

### Q-002: Node materials endpoint lacks file upload — BLOCKER
`POST /courses/{course_id}/nodes/{node_id}/materials` приймає тільки JSON з `source_url`. Для локальних файлів немає можливості upload'у — потрібно або:
- (a) додати multipart upload до нового endpoint (як у старому `POST /courses/{course_id}/materials`)
- (b) створити окремий upload endpoint що повертає S3 URL
- (c) presigned URL flow
Старий endpoint (`POST /courses/{course_id}/materials`) підтримує file upload, але прив'язує до course, не до node.

### Q-003: File Upload & Storage — Requirements for Full Solution
Повноцінна система завантаження матеріалів повинна підтримувати:

**Два шляхи завантаження:**
1. **URL** — передаємо зовнішнє посилання (YouTube, web page, hosted file)
2. **File upload** — заливаємо локальний файл → зберігаємо в B2 → обробляємо

**Обов'язкові вимоги:**
- Кожен матеріал має поле `source_url` — вказує де джерело (наш B2 або зовнішній URL)
- Якщо матеріал у нашому сховищі (B2):
  - Користувач бачить список **всіх своїх** матеріалів
  - Відображається **загальний обсяг** зайнятого місця (storage quota)
- **Security**: зовнішній доступ до матеріалів у B2 заборонений (private bucket, no public URLs)
  - Перевірити: presigned URLs? proxy endpoint? bucket policy?
  - Матеріали одного tenant не повинні бути доступні іншому

**Scope**: це виходить за рамки S2-058, потребує окремої задачі/epic.
