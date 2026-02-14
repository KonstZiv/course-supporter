# Epic 5: API Layer (FastAPI) ✅

## Мета

REST API для взаємодії з системою. Після цього епіку — можна через HTTP створити курс, завантажити матеріали, запустити Ingestion + ArchitectAgent, отримати структуровану програму курсу. Це "обличчя" системи для зовнішніх клієнтів.

## Передумови

- **Epic 1 ✅**: DB, config, infra
- **Epic 2 ✅**: ModelRouter, LLM providers
- **Epic 3 ✅**: Ingestion pipeline (SourceProcessors → MergeStep → CourseContext)
- **Epic 4 ✅**: ArchitectAgent (step-based: CourseContext → CourseStructure), CourseStructureRepository (→ DB), 55 тестів, 3 міграції

## Що зроблено

Шість задач, 54 тести:

1. **FastAPI bootstrap** (S1-023) ✅ — `api/app.py` з lifespan (DB pool, ModelRouter, S3Client init), health endpoint `/health`, CORS middleware (з `settings.cors_allowed_origins`), global error handler, structlog logging. 8 тестів.
2. **POST /courses** (S1-024) ✅ — створення курсу. `CourseRepository` з CRUD (create, get_by_id, list_all, get_with_structure). Pydantic schemas: `CourseCreateRequest`, `CourseResponse`. 10 тестів.
3. **POST /courses/{id}/materials** (S1-025) ✅ — завантаження матеріалів. `S3Client` (aiobotocore async context manager) для file upload. `ingest_material` background task з `PROCESSOR_MAP`. Multipart form: file або URL. 15 тестів (6 materials + 3 ingestion task + 6 s3 client).
4. **POST /courses/{id}/slide-mapping** (S1-026) ✅ — `SlideVideoMappingRepository.batch_create()`. Bulk insert з 201 response. 6 тестів.
5. **GET /courses/{id}** (S1-027) ✅ — nested eager loading через `selectinload` chains (avoids cartesian product). `CourseDetailResponse` з modules → lessons → concepts/exercises. 8 тестів.
6. **GET /courses/{id}/lessons/{lesson_id}** (S1-028) ✅ — `LessonRepository.get_by_id_for_course()` з JOIN Module для перевірки ownership. `LessonDetailResponse`. 7 тестів.

## Фінальна структура

```
src/course_supporter/api/
├── __init__.py           # Public: app
├── app.py                # FastAPI app, lifespan (DB + ModelRouter + S3Client), CORS, health, error handler
├── deps.py               # Dependencies: get_session (re-export), get_model_router, get_s3_client (cast())
├── schemas.py            # Request/Response Pydantic models
├── tasks.py              # Background: PROCESSOR_MAP, ingest_material()
└── routes/
    ├── __init__.py
    └── courses.py         # 5 endpoints on APIRouter(tags=["courses"])

src/course_supporter/storage/
├── s3.py                 # S3Client (aiobotocore, async context manager, ensure_bucket)
└── repositories.py       # +CourseRepository, +SlideVideoMappingRepository, +LessonRepository
```

## Контрольні точки

- [x] FastAPI app запускається з `uvicorn`, `/health` повертає `200`
- [x] CORS, error handlers, lifespan (DB + ModelRouter + S3Client) налаштовані
- [x] `POST /courses` створює курс у DB
- [x] `POST /courses/{id}/materials` приймає файл/URL, створює SourceMaterial
- [x] Background task запускає Ingestion pipeline
- [x] `POST /courses/{id}/slide-mapping` зберігає SlideVideoMapEntry → ORM
- [x] `GET /courses/{id}` повертає курс із повною вкладеною структурою
- [x] `GET /courses/{id}/lessons/{lesson_id}` повертає деталі уроку
- [x] Unit тести з `httpx.AsyncClient` (ASGITransport)
- [x] `make check` проходить (294 тести)

## Архітектурні рішення

- **`httpx.AsyncClient` + `ASGITransport`** для API тестів — без реального сервера
- **`app.dependency_overrides`** для мокання DB session, S3 client
- **`selectinload` chains** замість `joinedload` — уникаємо cartesian product для nested structures
- **`cast()` в deps.py** замість `type: ignore[no-any-return]` — чистіший типізований код
- **`PROCESSOR_MAP` dict** — mapping SourceType → ProcessorClass на рівні модуля
- **Two-session pattern** в `ingest_material` — основна сесія для processing, окрема для error handling (після rollback)
- **`elif` для type narrowing** — `elif source_url is not None:` замість `assert` для природного mypy narrowing
- **`async with` S3Client в lifespan** — клієнт живе весь час роботи додатку, `yield` всередині context manager
- **CORS з config** — `settings.cors_allowed_origins` з `["*"]` дефолтом для MVP

## Залежності

- **Блокується:** Epic 3 (Ingestion) ✅, Epic 4 (ArchitectAgent) ✅
- **Блокує:** Epic 6 (eval потребує API для end-to-end тестів)

## Задачі

| ID | Назва | Статус | Тести | Опис |
|:---|:---|:---|:---|:---|
| S1-023 | FastAPI bootstrap | ✅ | 8 | Lifespan (DB + ModelRouter + S3Client), CORS, health, error handler, structlog |
| S1-024 | POST /courses | ✅ | 10 | CourseRepository CRUD, CourseCreateRequest/CourseResponse, 201 |
| S1-025 | POST /courses/{id}/materials | ✅ | 15 | S3Client (aiobotocore), file upload + URL, background ingest_material |
| S1-026 | POST /courses/{id}/slide-mapping | ✅ | 6 | SlideVideoMappingRepository.batch_create(), bulk insert |
| S1-027 | GET /courses/{id} | ✅ | 8 | Nested selectinload chains, CourseDetailResponse |
| S1-028 | GET /courses/{id}/lessons/{id} | ✅ | 7 | LessonRepository, JOIN Module ownership, LessonDetailResponse |

**Загалом: 54 тести, 294 сумарно**

## Ризики (фактичний результат)

- **Background tasks** — використано FastAPI `BackgroundTasks` для MVP. Для production потрібен Celery/ARQ — залишається в backlog.
- **File uploads** — S3Client з aiobotocore, streaming upload не реалізовано (post-MVP).
- **N+1 queries** — вирішено через `selectinload` chains, працює ефективно.
- **aiobotocore typing** — бібліотека не типізована, використовується `Any` для client objects.
