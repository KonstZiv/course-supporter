     Epic 5: API Layer — Implementation Plan

     Context

     Epic 4 (Architect Agent) завершено — 240 тестів, 3 міграції. Вся інфраструктура готова: Config, DB
     (async_session + get_session dependency), ORM (8 таблиць), SourceMaterialRepository,
     CourseStructureRepository, ModelRouter, Ingestion pipeline, ArchitectAgent. API stubs порожні. Потрібно
     побудувати REST API для взаємодії з системою.

     Рішення користувача: URL + File upload (MinIO), prefix /api/v1.

     ---
     Task Breakdown & Order

     S1-023 (FastAPI bootstrap)           — блокує все
       ├→ S1-024 (POST /courses)          — CourseRepository, базовий CRUD
       │    ├→ S1-025 (POST /materials)   — S3Client, file upload, background ingestion
       │    └→ S1-026 (POST /slide-map)   — SlideVideoMappingRepository
       ├→ S1-027 (GET /courses/{id})      — eager loading, nested response
       └→ S1-028 (GET /lessons/{id})      — LessonRepository

     Порядок: S1-023 → S1-024 → S1-026 → S1-027 → S1-028 → S1-025

     S1-025 останнім — найскладніший (S3 client + background tasks + processor selection).

     ---
     S1-023: FastAPI Bootstrap (~8 тестів)

     Створити:
     - src/course_supporter/api/app.py — FastAPI app, lifespan, CORS, health, global error handler
     - src/course_supporter/api/deps.py — FastAPI dependencies (re-export get_session, get_model_router)
     - tests/unit/test_api/__init__.py
     - tests/unit/test_api/test_health.py

     Змінити:
     - src/course_supporter/api/__init__.py — re-export app
     - Makefile — додати run-api target

     Ключові рішення:

     Lifespan — імпортує існуючі engine, async_session з storage/database.py, створює ModelRouter в app.state:
     @asynccontextmanager
     async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
         app.state.model_router = create_model_router(settings, async_session)
         yield
         await engine.dispose()

     CORS — allow_origins=["*"] для MVP.

     Health — GET /health → {"status": "ok"}.

     Global error handler — ловить unhandled exceptions, логує structlog, повертає 500.

     Тести: health 200, CORS headers, 404 unknown route, 500 on unhandled exception, lifespan model_router
     creation.

     ---
     S1-024: POST /courses (~10 тестів)

     Створити:
     - src/course_supporter/api/schemas.py — request/response Pydantic models
     - tests/unit/test_api/test_courses_create.py

     Змінити:
     - src/course_supporter/storage/repositories.py — додати CourseRepository
     - src/course_supporter/api/routes/courses.py — замінити stub на APIRouter

     CourseRepository (в repositories.py):
     - create(title, description) -> Course
     - get_by_id(course_id) -> Course | None
     - list_all(limit, offset) -> list[Course]

     Schemas:
     - CourseCreateRequest — {title: str, description: str | None}
     - CourseResponse — {id, title, description, created_at, updated_at} з from_attributes=True

     Route: POST /api/v1/courses → 201 + CourseResponse.

     Тести: create success (201), empty title (422), title too long (422), with/without description, repo
     create/get_by_id/list_all.

     ---
     S1-026: POST /courses/{id}/slide-mapping (~7 тестів)

     Створити:
     - tests/unit/test_api/test_slide_mapping.py

     Змінити:
     - src/course_supporter/storage/repositories.py — додати SlideVideoMappingRepository
     - src/course_supporter/api/schemas.py — додати request/response
     - src/course_supporter/api/routes/courses.py — додати endpoint

     SlideVideoMappingRepository:
     - batch_create(course_id, mappings) -> list[SlideVideoMapping]
     - get_by_course_id(course_id) -> list[SlideVideoMapping]

     Route: POST /api/v1/courses/{course_id}/slide-mapping → 201. Перевіряє існування course (404).

     Тести: success (201), course not found (404), empty list (422), repo batch_create/get_by_course_id.

     ---
     S1-027: GET /courses/{id} (~8 тестів)

     Створити:
     - tests/unit/test_api/test_courses_detail.py

     Змінити:
     - src/course_supporter/storage/repositories.py — додати get_with_structure() до CourseRepository
     - src/course_supporter/api/schemas.py — nested response models
     - src/course_supporter/api/routes/courses.py — додати endpoint

     Eager loading через selectinload chains (не joinedload — уникаємо cartesian product):
     selectinload(Course.source_materials),
     selectinload(Course.modules)
         .selectinload(Module.lessons)
         .selectinload(Lesson.concepts),
     selectinload(Course.modules)
         .selectinload(Module.lessons)
         .selectinload(Lesson.exercises),

     Nested response schemas:
     - CourseDetailResponse — course + modules[] + source_materials[]
     - ModuleResponse — module + lessons[]
     - LessonResponse — lesson + concepts[] + exercises[]
     - ConceptResponse, ExerciseResponse, SourceMaterialResponse

     Всі з from_attributes=True.

     Route: GET /api/v1/courses/{course_id} → 200 або 404.

     Тести: success (200), not found (404), empty structure, includes source_materials, nested
     modules→lessons→concepts.

     ---
     S1-028: GET /courses/{id}/lessons/{lesson_id} (~7 тестів)

     Створити:
     - tests/unit/test_api/test_lesson_detail.py

     Змінити:
     - src/course_supporter/storage/repositories.py — додати LessonRepository
     - src/course_supporter/api/schemas.py — LessonDetailResponse
     - src/course_supporter/api/routes/courses.py — додати endpoint

     LessonRepository:
     - get_by_id_for_course(lesson_id, course_id) -> Lesson | None — JOIN Module для перевірки ownership +
     selectinload concepts/exercises

     Route: GET /api/v1/courses/{course_id}/lessons/{lesson_id} → 200 або 404.

     Тести: success (200), lesson not found (404), course not found (404), wrong course (404), includes
     concepts+exercises, repo tests.

     ---
     S1-025: POST /courses/{id}/materials (~12 тестів)

     Найскладніша задача — S3 client + file upload + background ingestion.

     Створити:
     - src/course_supporter/storage/s3.py — async S3 client (aiobotocore)
     - src/course_supporter/api/tasks.py — background ingestion task
     - tests/unit/test_api/test_materials.py
     - tests/unit/test_s3_client.py

     Змінити:
     - pyproject.toml — додати aiobotocore dependency
     - src/course_supporter/api/schemas.py — material schemas
     - src/course_supporter/api/routes/courses.py — додати endpoint
     - src/course_supporter/api/deps.py — додати get_s3_client dependency
     - src/course_supporter/api/app.py — S3 client в lifespan

     S3Client (storage/s3.py):
     class S3Client:
         async def upload_file(self, key: str, data: bytes, content_type: str) -> str:
             """Upload file to S3, return the object URL."""
         async def close(self) -> None:
             """Close the S3 session."""

     Ініціалізується в lifespan, зберігається в app.state.s3_client.

     Endpoint: POST /api/v1/courses/{course_id}/materials

     Два варіанти використання (multipart form):
     - file: UploadFile + source_type: str — файл завантажується в MinIO, source_url = S3 object URL
     - source_url: str + source_type: str — URL зберігається напряму

     Background task (api/tasks.py):
     1. Нова сесія з async_session()
     2. update_status(material_id, "processing")
     3. Select processor by source_type
     4. processor.process(...) → SourceDocument
     5. update_status(material_id, "done", content_snapshot=...)
     6. Error handling: update_status(material_id, "error", error_message=...)

     Тести: create with URL (201), create with file upload (201), course not found (404), invalid source_type
     (422), returns pending status, background task called, s3_client upload/close, ingestion success/error.

     ---
     Summary

     Task: S1-023
     Files Create: app.py, deps.py, test_health.py
     Files Modify: api/init.py, Makefile
     Tests: ~8
     ────────────────────────────────────────
     Task: S1-024
     Files Create: schemas.py, test_courses_create.py
     Files Modify: repositories.py, routes/courses.py
     Tests: ~10
     ────────────────────────────────────────
     Task: S1-026
     Files Create: test_slide_mapping.py
     Files Modify: repositories.py, schemas.py, routes/courses.py
     Tests: ~7
     ────────────────────────────────────────
     Task: S1-027
     Files Create: test_courses_detail.py
     Files Modify: repositories.py, schemas.py, routes/courses.py
     Tests: ~8
     ────────────────────────────────────────
     Task: S1-028
     Files Create: test_lesson_detail.py
     Files Modify: repositories.py, schemas.py, routes/courses.py
     Tests: ~7
     ────────────────────────────────────────
     Task: S1-025
     Files Create: s3.py, tasks.py, test_materials.py, test_s3_client.py
     Files Modify: pyproject.toml, schemas.py, routes/courses.py, deps.py, app.py
     Tests: ~12
     ────────────────────────────────────────
     Task: Total
     Files Create: 10 new
     Files Modify: 8 modified
     Tests: ~52

     After Epic 5: ~240 + ~52 = ~292 тестів.

     ---
     Testing Strategy

     - httpx.AsyncClient з ASGITransport(app=app) — без реального сервера
     - Mock DB session через app.dependency_overrides[get_session]
     - Mock S3 client через app.dependency_overrides[get_s3_client]
     - Background tasks: mock BackgroundTasks.add_task, unit test task function окремо
     - Pattern: fixtures повертають AsyncMock(spec=AsyncSession), override dependencies

     ---
     Verification

     Після кожної задачі:
     make check           # ruff + mypy + pytest
     uv run pytest -q     # quick test run

     Після всіх задач:
     docker compose up -d         # postgres + minio running
     make db-upgrade              # apply migrations
     uv run uvicorn course_supporter.api:app --reload
     # Test manually:
     curl http://localhost:8000/health
     curl -X POST http://localhost:8000/api/v1/courses -H 'Content-Type: application/json' -d '{"title":"Test"}'
