# Epic 5: API Layer (FastAPI)

## Мета

REST API для взаємодії з системою. Після цього епіку — можна через HTTP створити курс, завантажити матеріали, запустити Ingestion + ArchitectAgent, отримати структуровану програму курсу. Це "обличчя" системи для зовнішніх клієнтів.

## Передумови

- **Epic 1 ✅**: DB, config, infra
- **Epic 2 ✅**: ModelRouter, LLM providers
- **Epic 3 ✅**: Ingestion pipeline (SourceProcessors → MergeStep → CourseContext)
- **Epic 4 ✅**: ArchitectAgent (step-based: CourseContext → CourseStructure), CourseStructureRepository (→ DB), 55 тестів, 3 міграції

## Що робимо

Шість задач:

1. **FastAPI bootstrap** (S1-023) — `api/app.py` з lifespan (DB pool, ModelRouter init), health endpoint `/health`, CORS middleware, error handlers. `uvicorn` entrypoint.
2. **POST /courses** (S1-024) — створення курсу. Request: `{title, description}`. Response: `Course` з `id`. Зберігає в DB через repository.
3. **POST /courses/{id}/materials** (S1-025) — завантаження матеріалів. Приймає `UploadFile` або URL. Створює `SourceMaterial`, запускає Ingestion pipeline (background task). Response: `SourceMaterial` зі статусом `pending`.
4. **POST /courses/{id}/slide-mapping** (S1-026) — завантаження slide-video mapping. Request: `list[SlideVideoMapEntry]`. Response: створені `SlideVideoMapping` записи.
5. **GET /courses/{id}** (S1-027) — отримання курсу з повною структурою (modules → lessons → concepts → exercises). Response: nested JSON. Includes source_materials status.
6. **GET /courses/{id}/lessons/{lesson_id}** (S1-028) — деталі уроку з concepts, exercises, cross-references. Response: `LessonDetail` з nested concepts та exercises.

## Для чого

API — єдиний спосіб взаємодії зовнішніх клієнтів із системою. Без нього Ingestion + ArchitectAgent працюють лише через скрипти. FastAPI дає автоматичну OpenAPI документацію, валідацію request/response, async підтримку.

## Контрольні точки

- [ ] FastAPI app запускається з `uvicorn`, `/health` повертає `200`
- [ ] CORS, error handlers, lifespan (DB + ModelRouter) налаштовані
- [ ] `POST /courses` створює курс у DB
- [ ] `POST /courses/{id}/materials` приймає файл/URL, створює SourceMaterial
- [ ] Background task запускає Ingestion pipeline
- [ ] `POST /courses/{id}/slide-mapping` зберігає SlideVideoMapEntry → ORM
- [ ] `GET /courses/{id}` повертає курс із повною вкладеною структурою
- [ ] `GET /courses/{id}/lessons/{lesson_id}` повертає деталі уроку
- [ ] Unit + integration тести з `httpx.AsyncClient` (TestClient)
- [ ] `make check` проходить

## Залежності

- **Блокується:** Epic 3 (Ingestion) ✅, Epic 4 (ArchitectAgent) ✅
- **Блокує:** Epic 6 (eval потребує API для end-to-end тестів)

## Задачі

| ID | Назва | Естімейт | Примітка |
|:---|:---|:---|:---|
| S1-023 | FastAPI bootstrap | 0.5 дня | lifespan, health, CORS, errors |
| S1-024 | POST /courses | 0.5 дня | CourseRepository.create() |
| S1-025 | POST /courses/{id}/materials | 1 день | Upload + background Ingestion |
| S1-026 | POST /courses/{id}/slide-mapping | 0.5 дня | Batch create SlideVideoMapping |
| S1-027 | GET /courses/{id} | 0.5 дня | Nested eager loading |
| S1-028 | GET /courses/{id}/lessons/{lesson_id} | 0.5 дня | Lesson detail with cross-refs |

**Загалом: ~3.5 дні**

## Ризики

- **Background tasks** — FastAPI `BackgroundTasks` підходить для MVP, але для production потрібен Celery/ARQ. Мітигація: абстрагувати за інтерфейсом.
- **File uploads** — великі відео (~1GB) можуть timeout. Мітигація: streaming upload, S3 presigned URLs (post-MVP).
- **N+1 queries** — nested course structure. Мітигація: `selectinload` / `joinedload` для eager loading.
