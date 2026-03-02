# S2-059: Endpoint Reference (Layer 3) — Деталі для виконавця

**Epic:** EPIC-7 — Integration Documentation + Manual QA

---

## Контекст

Третя і найоб'ємніша задача epic. Повна довідка по всіх endpoints. На відміну від Quick Start (один happy path), тут описуються всі варіації, edge cases, error responses. Кожен варіант тестується на production.

---

## Деталі по групах

### 1. `auth.md` — Authentication & Authorization

- API Key format і як отримати
- Header: `X-API-Key`
- Scopes: `prep` (read+write), `check` (read-only)
- Rate limits: per tenant + scope, 60s window
- Response при невалідному ключі (401)
- Response при expired ключі (401)
- Response при недостатньому scope (403)
- Response при rate limit (429 + Retry-After)
- Tenant isolation — пояснення

### 2. `courses.md` — Course Management (3 endpoints)

**POST /api/v1/courses**
- Create course: title (required), description (optional)
- Варіації: мінімальний (тільки title), повний (title + description)
- Errors: 422 (validation), 401, 429

**GET /api/v1/courses**
- List courses з пагінацією
- Варіації: default, custom limit/offset, порожній список
- Query params: limit (1-100, default 20), offset (>=0)

**GET /api/v1/courses/{course_id}**
- Повна деталь: nested structure, material tree, fingerprints
- Errors: 404 (не знайдено), 404 (чужий tenant)

### 3. `nodes.md` — Material Tree (8 endpoints)

**POST .../nodes** — create root node
**POST .../nodes/{id}/children** — create child
- Варіації: root, 1 рівень, глибока вкладеність

**GET .../nodes/tree** — повне дерево (recursive)
**GET .../nodes/{id}** — single node (flat)

**PATCH .../nodes/{id}** — partial update
- Варіації: тільки title, тільки description, обидва

**POST .../nodes/{id}/move** — move node
- Варіації: move to root (parent_id=null), move to parent
- Error: cycle detection

**POST .../nodes/{id}/reorder** — reorder siblings
- Варіації: move to 0, move to last, move to middle, auto-clamp

**DELETE .../nodes/{id}** — cascade delete
- Перевірити: cascade на матеріали та дочірні nodes

### 4. `materials.md` — Materials (6 endpoints)

**POST .../nodes/{id}/materials** — add material to node
- Варіації per source_type:
  - text: .md, .docx, .html, .txt (file upload)
  - presentation: .pdf, .pptx (file upload)
  - video: .mp4, .webm, .mkv, .avi (file upload)
  - web: source_url (no file)
- File validation errors
- Auto-enqueue ingestion (job_id в response)

**GET .../nodes/{id}/materials** — list materials per node
**GET .../materials/{id}** — single material with derived state

**DELETE .../materials/{id}** — delete material

**POST .../materials/{id}/retry** — retry failed ingestion
- Errors: 422 якщо state != "error"

**POST /courses/{id}/materials** (legacy) — direct upload without node

### 5. `mappings.md` — Slide-Video Mappings (3 endpoints)

**POST .../nodes/{id}/slide-mapping** — batch create
- Partial success pattern: 201 / 207 / 422
- Validation levels: L1 (structural), L2 (content), L3 (deferred)
- Natural key dedup: (pres_id, vid_id, slide_number, tc_start)
- Варіації: all valid, mixed, all invalid, duplicates

**GET .../nodes/{id}/slide-mapping** — list per node

**DELETE .../slide-mapping/{id}** — delete single

### 6. `generation.md` — Structure Generation (4 endpoints)

**POST .../generate** — trigger generation
- Modes: free (default), guided
- Response codes: 200 (existing), 202 (enqueued), 404, 409 (conflict), 422 (no ready materials)
- Варіації: free mode, guided mode with node_ids
- Cascade: auto-enqueue stale ingestion before generation

**GET .../structure** — latest snapshot
- Query param: node_id (optional, для subtree)

**GET .../structure/history** — paginated snapshot list
**GET .../structure/snapshots/{id}** — specific snapshot

### 7. `jobs.md` — Job Status (1 endpoint)

**GET /api/v1/jobs/{job_id}**
- Job types: ingestion, generation
- Statuses: queued → active → complete / failed
- Tenant isolation через course→tenant chain
- Polling pattern: recommended interval, timeout

### 8. `reports.md` — Cost Reports (1 endpoint)

**GET /api/v1/reports/cost**
- Summary + provider breakdowns
- LLM call tracking (tokens, cost)

### 9. `errors.md` — Error Handling Guide

- Standard error response format
- HTTP status code reference:
  - 400: Bad Request
  - 401: Unauthorized (invalid/expired key)
  - 403: Forbidden (insufficient scope)
  - 404: Not Found (resource or tenant mismatch)
  - 409: Conflict (generation in progress)
  - 422: Unprocessable Entity (validation errors, no ready materials)
  - 429: Too Many Requests (rate limit + Retry-After)
  - 500: Internal Server Error
  - 503: Service Unavailable (health check degraded)
- Retry strategies per error type
- Polling pattern (interval, backoff, timeout)

---

## Тест-матриця

Для кожного endpoint:

| Аспект | Що тестуємо |
|--------|-------------|
| Happy path | Основний запит з валідними даними |
| Variations | Різні комбінації параметрів |
| Validation errors | Невалідні/відсутні обов'язкові поля |
| Not found | Неіснуючий ресурс |
| Tenant isolation | Ресурс іншого tenant |
| Auth errors | Невалідний key, expired, wrong scope |
| Rate limit | Багато запитів поспіль |
| Edge cases | Порожні списки, граничні значення, великі payload |

---

## Bug Tracking

Аналогічно S2-058 — кожен баг фіксується з номером, описом, фіксом і тестом.

---

## Checklist

- [ ] Всі 9 файлів документації створені
- [ ] Всі 29 endpoints описані
- [ ] Кожен endpoint має curl-приклад
- [ ] Error responses документовані
- [ ] Варіації протестовані на production
- [ ] Знайдені баги виправлені і покриті тестами
- [ ] `mkdocs build --strict` проходить
- [ ] Перехресні посилання між документами працюють

---

## Нотатки

_Простір для нотаток виконавця під час роботи над задачею._
