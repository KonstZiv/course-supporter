# Sprint 2 — Material Tree, Task Queue, Structure Generation

**Статус:** DRAFT v4 — в обговоренні
**Попередній спрінт:** Sprint 1 (Materials-to-Structure MVP, 326 тестів, deploy на api.pythoncourse.me)
**Оцінка:** 4-5 тижнів
**Ціль:** Документація проєкту + інтуїтивний flow для роботи з курсами + production-ready обробка з чергою + per-node генерація структури

---

## Контекст і мотивація

Sprint 1 дав працюючий MVP: завантаження матеріалів → ingestion → ArchitectAgent → структура курсу. Але:

1. **Немає документації проєкту** — архітектурні рішення, ERD, sprint history живуть в розрізнених файлах, немає єдиного джерела правди
2. **Немає інтуїтивного flow** — плоский список матеріалів, немає ієрархії, немає explicit trigger генерації структури
2. **Fire-and-forget обробка** — `BackgroundTasks` без черги, без контролю concurrency, втрата задач при рестарті
3. **Heavy ops не ізольовані** — whisper/vision зашиті в процесори, неможливо винести на serverless
4. **Немає контролю версій** — не зрозуміло чи структура курсу відповідає поточному набору матеріалів
5. **Зовнішня команда чекає** — потрібна документація flow + endpoints для початку інтеграції

---

## Архітектурні рішення

### AR-1: MaterialTree (recursive adjacency list)

Довільна ієрархія вузлів. Матеріали можуть належати будь-якому вузлу (не тільки листкам):

```
Course "Python для початківців"
  ├── 📄 syllabus.pdf                       ← матеріал на рівні курсу
  ├── 📁 "Вступ до Python"
  │     ├── 📄 intro-video.mp4              ← матеріал на рівні секції
  │     ├── 📁 "Типи даних"
  │     │     ├── 📄 types-slides.pdf       ← і на підрівні
  │     │     └── 📄 types-article.html
  │     └── 📁 "Цикли"
  │           └── 📄 loops-video.mp4
  └── 📁 "Web-розробка"
        └── 📄 django-overview.pdf
```

ORM: `MaterialNode(id, course_id, parent_id → self, title, description, order)`.

**Обґрунтування:** фіксовані рівні (Course → Module → Topic) не покривають довільні учбові конструкції. Adjacency list — найпростіша реалізація для async SQLAlchemy. PostgreSQL `WITH RECURSIVE` доступний для складних запитів, але для типових глибин 3-5 рівнів достатньо eager loading.

**Tenant isolation:** успадковується через FK ланцюжок `MaterialEntry → MaterialNode → Course(tenant_id)`. Всі нові endpoints перевіряють належність курсу tenant-у через `CourseRepository.get_by_id()` перед доступом до nodes/materials.

### AR-2: MaterialEntry (замість SourceMaterial)

Замість одного `SourceMaterial` що змішує raw, processed і status — розділення на чіткі шари з "квитанцією" про відправку на обробку:

```python
class MaterialEntry(Base):
    __tablename__ = "material_entries"

    id: Mapped[uuid.UUID]
    node_id: Mapped[uuid.UUID]              # FK → material_nodes
    source_type: Mapped[str]                # video/presentation/text/web
    order: Mapped[int]

    # ── Raw layer ──
    source_url: Mapped[str]                 # S3 URL або зовнішній URL
    filename: Mapped[str | None]
    raw_hash: Mapped[str | None]            # lazy cached, sha256 контенту
    raw_size_bytes: Mapped[int | None]

    # ── Processed layer ──
    processed_hash: Mapped[str | None]      # для якого raw_hash зроблена обробка
    processed_content: Mapped[str | None]   # SourceDocument JSON
    processed_at: Mapped[datetime | None]

    # ── Pending "receipt" ──
    pending_job_id: Mapped[uuid.UUID | None]  # FK → jobs (сліди відправки)
    pending_since: Mapped[datetime | None]     # коли відправлено

    # ── Fingerprint ──
    content_fingerprint: Mapped[str | None]  # lazy cached, sha256(processed_content)

    # ── Errors ──
    error_message: Mapped[str | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
```

**Стан — derived property:**

```python
class MaterialState(StrEnum):
    RAW = "raw"                             # завантажено, не оброблено
    PENDING = "pending"                     # відправлено на обробку (є квитанція)
    READY = "ready"                         # оброблено, hash збігається
    INTEGRITY_BROKEN = "integrity_broken"   # raw змінився після обробки
    ERROR = "error"                         # обробка зафейлилась

@property
def state(self) -> MaterialState:
    if self.error_message:
        return MaterialState.ERROR
    if self.pending_job_id is not None:
        return MaterialState.PENDING
    if self.processed_content is None:
        return MaterialState.RAW
    if self.raw_hash and self.processed_hash != self.raw_hash:
        return MaterialState.INTEGRITY_BROKEN
    return MaterialState.READY
```

**Lifecycle:**

```
Upload file     → raw_hash=NULL, pending_job_id=NULL          → RAW
Start ingestion → pending_job_id=job_123, pending_since=now   → PENDING
Job completes   → processed_content=..., processed_hash=abc,
                   pending_job_id=NULL, pending_since=NULL     → READY
Job fails       → error_message="...", pending_job_id=NULL    → ERROR
Re-upload file  → raw_hash=NULL (invalidated)                 → INTEGRITY_BROKEN
```

**`raw_hash` — lazy cached property:**

- При file upload: рахується потоково під час upload в S3 (sha256 по chunks), одразу заповнений
- При URL: NULL до першого запиту. Рахується при потребі (fingerprint, старт обробки)
- При будь-якій модифікації raw-частини: скидається в NULL (invalidation)

**Квитанція PENDING дає:**
- `pending_since = 40 хв тому` → підозріло → `GET /jobs/{pending_job_id}` → бачиш проблему
- Dashboard: "3 матеріали в обробці, найдовший чекає 25 хв"

### AR-3: Task Queue (ARQ + Redis)

Замінити `BackgroundTasks` на ARQ:
- Redis для persistence (job-и не втрачаються при рестарті)
- `max_jobs` — контроль concurrency (whisper жере CPU/RAM)
- Retry з backoff для transient errors
- Job status tracking (queued → active → complete/failed)
- Job dependencies (`depends_on` — structure generation чекає ingestion)

**Інфраструктура:** +1 Redis контейнер (~50MB RAM), +1 worker process (~100-200MB).

#### Робоче вікно (Work Window)

Heavy jobs (whisper, vision, OCR) виконуються лише в конфігурованому часовому вікні. Light jobs (fingerprint, LLM calls) — завжди.

Реалізація: worker завжди живий, перед виконанням heavy job перевіряє вікно:

```python
async def execute_heavy_task(ctx, material_id, ...):
    window = get_work_window()
    if not window.is_active_now():
        raise Retry(defer=window.next_start())
    # ... actual work
```

#### Job Priorities

```python
class JobPriority(StrEnum):
    IMMEDIATE = "immediate"   # ігнорує вікно, виконується завжди
    NORMAL = "normal"         # чекає робочого вікна

# ingestion з whisper      → NORMAL (чекає вікно)
# fingerprint calculation  → IMMEDIATE
# structure generation     → IMMEDIATE (LLM call, не heavy compute)
```

#### Queue Estimates

При submit job — розрахунок estimated start/complete з урахуванням:
1. Позиція в черзі
2. Середній час виконання (з jobs history)
3. Робоче вікно (якщо поза вікном — коли відкриється + час черги)

```python
@dataclass
class QueueEstimate:
    position_in_queue: int
    estimated_start: datetime
    estimated_complete: datetime
    next_window_start: datetime | None   # якщо зараз поза вікном
    queue_summary: str                   # "5 завдань в черзі, вікно 02:00-06:30"
```

#### Конфігурація через env

```python
class Settings(BaseSettings):
    # ... existing ...

    # Worker
    worker_max_jobs: int = 2
    worker_heavy_window_start: str = "02:00"     # HH:MM
    worker_heavy_window_end: str = "06:30"       # HH:MM
    worker_heavy_window_enabled: bool = True      # False = 24/7
    worker_heavy_window_tz: str = "UTC"
    worker_job_timeout: int = 1800               # секунд (30 хв default)
    worker_max_tries: int = 3
    worker_immediate_override: bool = True        # дозволити priority: immediate
```

#### Ingestion completion callback

Після завершення ingestion job (success або failure) — worker виконує callback:

1. Оновлює `MaterialEntry` (processed_content, status, etc.)
2. Інвалідує fingerprints вгору по дереву
3. **Trigger revalidation** маппінгів що очікують цей матеріал (AR-7)

### AR-4: Merkle Fingerprints

Двохрівнева система fingerprint з каскадною інвалідацією знизу вгору:

**Material fingerprint** (`content_fingerprint`): sha256(processed_content). Lazy cached в `MaterialEntry`.

**Node fingerprint** (`node_fingerprint`): hash від fingerprints вкладених матеріалів + fingerprints дочірніх nodes. Lazy cached в `MaterialNode`.

**Course fingerprint**: hash від fingerprints root nodes.

```
Course fingerprint: hash(Node_A.fp + Node_B.fp + syllabus.mat_fp)
├── 📄 syllabus.pdf          mat_fp: sha256(processed_content)
├── 📁 Node A                node_fp: hash(video.mat_fp + Node_A1.fp)
│     ├── 📄 video.mp4       mat_fp: sha256(processed_content)
│     └── 📁 Node A1         node_fp: hash(slides.mat_fp + article.mat_fp)
│           ├── 📄 slides.pdf   mat_fp: sha256(processed_content)
│           └── 📄 article.html mat_fp: sha256(processed_content) ← ЗМІНИЛИ
└── 📁 Node B                node_fp: hash(django.mat_fp)
      └── 📄 django.pdf      mat_fp: sha256(processed_content)
```

Змінили `article.html` → інвалідується `Node_A1.fp` → `Node_A.fp` → `Course.fp`. `Node_B.fp` не змінився.

**Розрахунок (lazy, знизу вгору):**

```python
class FingerprintService:
    async def ensure_node_fp(self, node: MaterialNode) -> str:
        if node.node_fingerprint is not None:
            return node.node_fingerprint
        parts: list[str] = []
        for entry in sorted(node.materials, key=lambda e: e.id):
            fp = await self.ensure_material_fp(entry)
            parts.append(f"m:{fp}")
        for child in sorted(node.children, key=lambda c: c.id):
            fp = await self.ensure_node_fp(child)
            parts.append(f"n:{fp}")
        node.node_fingerprint = sha256("|".join(parts).encode()).hexdigest()
        await self._session.flush()
        return node.node_fingerprint
```

**Інвалідація — каскад вгору:** будь-яка модифікація матеріалу або вузла скидає `content_fingerprint` / `node_fingerprint` від точки зміни до кореня.

**API response — точкова діагностика:**
`fingerprint: null` на будь-якому рівні → щось змінилось нижче. Drill-down до конкретного матеріалу.

### AR-5: Heavy Steps Extraction (serverless-ready)

Розділення на heavy (serverless-ready) і light (on-premise) операції:

| Heavy (serverless-ready) | Light (on-premise) |
|---|---|
| whisper transcription | merge documents |
| slide/image → description (vision) | architect agent (LLM call) |
| PDF OCR | fingerprint calculation |
| video frame extraction | CRUD, status management |

Кожен heavy step — injectable callable з чистим контрактом:

```python
TranscribeFunc = Callable[[str, TranscribeParams], Awaitable[Transcript]]
DescribeSlidesFunc = Callable[[str, VisionParams], Awaitable[list[SlideDescription]]]
```

SourceProcessor стає оркестратором: готує input → викликає heavy step → пакує результат.
Коли прийде Lambda — міняємо лише implementation heavy step.

### AR-6: Structure Generation — per-node, каскадна

Генерація може бути запущена для будь-якого рівня дерева. Каскадно обробляє все піддерево від target node вниз.

**Endpoints:**

```
POST /api/v1/courses/{course_id}/structure/generate              → весь курс
POST /api/v1/courses/{course_id}/nodes/{node_id}/structure/generate → піддерево
```

`course_id` в шляху — для tenant isolation. `node_id` однозначно ідентифікує вузол на будь-якій глибині.

**Два режими:**
- **"free"** — методист будує оптимальну структуру сам. Input tree — лише context.
- **"guided"** — методист зберігає input tree як constraint, збагачує його.

**Каскадна логіка:**

```python
async def generate_for_subtree(self, course_id, node_id=None, mode="free"):
    stale_materials = await self._find_stale_materials(node_id)  # RAW, INTEGRITY_BROKEN
    if stale_materials:
        ingestion_jobs = [await enqueue("ingest_material", m.id) for m in stale_materials]
        structure_job = await enqueue("generate_structure", node_id, mode,
                                     depends_on=ingestion_jobs)
    else:
        current_fp = await self.fp_service.ensure_node_fp(root_node)
        existing = await self.snapshot_repo.find(node_id, current_fp, mode)
        if existing:
            return existing  # 200 OK — idempotent
        structure_job = await enqueue("generate_structure", node_id, mode)
    return structure_job  # 202 Accepted
```

**Conflict detection — перетин піддерев:**

409 Conflict виникає тільки коли нова генерація перетинається з active job:

| Active job scope | Новий запит | Результат |
|---|---|---|
| Course (all) | Node A | 409 — Node A вкладений |
| Node A | Node A1 | 409 — A1 вкладений в A |
| Node A | Node B | 202 — незалежні піддерева |
| Node A1 | Node A2 | 202 — siblings |

**Snapshot per-node:**

```python
class CourseStructureSnapshot(Base):
    course_id: Mapped[uuid.UUID]
    node_id: Mapped[uuid.UUID | None]      # NULL = весь курс
    node_fingerprint: Mapped[str]           # Merkle hash at generation time
    mode: Mapped[str]                       # free | guided
    structure: Mapped[dict]                 # результат
```

Idempotency: unique на `(course_id, node_id, node_fingerprint, mode)`.

**Apply snapshot → normalized tables:** коли snapshot "застосовується", його `structure` JSONB розпаковується в `modules` → `lessons` → `concepts` → `exercises`. `Module.snapshot_id` FK явно пов'язує активну структуру з джерельним snapshot.

**Response codes:**

```
200 OK            — snapshot з таким fingerprint+mode вже існує (idempotent)
202 Accepted      — job створено (з планом ingestion + estimate)
409 Conflict      — active job перетинається з запитаним scope
422 Unprocessable — немає жодного READY матеріалу в scope
```

**202 response — повна картина:**

```json
{
  "structure_job_id": "job-100",
  "scope": {"node_id": "node-A", "title": "Вступ до Python", "depth": 2},
  "plan": {
    "ingestion_required": [
      {"material_id": "...", "filename": "video.mp4", "state": "raw", "job_id": "job-101"}
    ],
    "already_ready": 7,
    "total_materials": 9
  },
  "estimate": {
    "position_in_queue": 6,
    "estimated_start": "2025-02-20T02:45:00Z",
    "estimated_complete": "2025-02-20T03:10:00Z",
    "next_window_start": "2025-02-20T02:00:00Z",
    "summary": "5 завдань в черзі, обробка 02:00–06:30 UTC"
  },
  "warnings": [
    {"material_id": "...", "state": "error", "filename": "broken.pdf", "message": "Не включено — потребує retry"}
  ]
}
```

**Structure generation враховує validation_state маппінгів** — якщо в scope є маппінги з `pending_validation` або `validation_failed`, це включається в warnings response.

### AR-7: SlideVideoMapping — explicit references + deferred validation

Маппінг зв'язує **конкретну презентацію** з **конкретним відео** через FK на `MaterialEntry`. Один слайд може з'являтись в різних відео, одне відео може містити слайди з різних презентацій.

```
Node "Вступ до Python"
├── 📄 lecture-1.mp4          (vid-1)
├── 📄 lecture-2.mp4          (vid-2)
├── 📄 basics-slides.pdf      (pres-1)
└── 📄 advanced-slides.pdf    (pres-2)

Mappings:
  pres-1, slide 3  → vid-1, 00:05:30–00:08:15
  pres-1, slide 3  → vid-2, 00:42:00–00:43:30   ← той самий слайд в іншому відео
  pres-2, slide 1  → vid-1, 00:15:00–00:18:45
```

**ORM:**

```python
class SlideVideoMapping(Base):
    __tablename__ = "slide_video_mappings"

    id: Mapped[uuid.UUID]
    node_id: Mapped[uuid.UUID]                   # FK → material_nodes
    presentation_entry_id: Mapped[uuid.UUID]      # FK → material_entries
    video_entry_id: Mapped[uuid.UUID]             # FK → material_entries
    slide_number: Mapped[int]
    video_timecode_start: Mapped[str]
    video_timecode_end: Mapped[str | None]
    order: Mapped[int]

    # Deferred validation
    validation_state: Mapped[str]                 # validated | pending_validation | validation_failed
    blocking_factors: Mapped[list[dict] | None]   # JSONB
    validation_errors: Mapped[list[dict] | None]  # JSONB
    validated_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
```

**Трирівнева валідація:**

**Рівень 1 — Структурна (при створенні, завжди):**
- Обидва матеріали існують і належать цьому node
- `source_type` правильний (presentation / video)
- Timecode format валідний
- **Помилка тут → mapping не створюється, детальне повідомлення з hint**

**Рівень 2 — Контентна (якщо матеріали READY):**
- `slide_number` в межах кількості слайдів презентації
- `video_timecode` в межах тривалості відео
- **Помилка тут → mapping не створюється, повідомлення з допустимими діапазонами**

**Рівень 3 — Відкладена (якщо матеріали не READY):**
- Маппінг створюється зі статусом `pending_validation`
- `blocking_factors` описують що саме блокує перевірку
- **Автоматична revalidation** коли блокуючий матеріал завершує ingestion

**Validation state lifecycle:**

```
Створення:
  Обидва READY   → Рівень 1+2 → VALIDATED або VALIDATION_FAILED
  Не всі READY   → Рівень 1   → PENDING_VALIDATION + blocking_factors

Ingestion complete (callback):
  Матеріал → READY  → revalidate → блокер знятий → повна валідація якщо всі зняті
  Матеріал → ERROR  → revalidate → блокер оновлюється (material_error)

Retry ingestion:
  Матеріал → PENDING → revalidate → блокер повертається до material_not_ready
```

**Blocking factors — JSONB приклади:**

```json
// Матеріал ще обробляється
[{
  "type": "material_not_ready",
  "material_entry_id": "pres-1",
  "filename": "basics.pdf",
  "material_state": "pending",
  "message": "Перевірка slide_number заблокована: 'basics.pdf' на обробці",
  "blocked_checks": ["slide_number_range"]
}]

// Обробка зафейлила
[{
  "type": "material_error",
  "material_entry_id": "pres-1",
  "filename": "basics.pdf",
  "error": "PDF parsing failed: corrupted file",
  "message": "Перевірка заблокована: обробка 'basics.pdf' завершилась помилкою. Перевірка буде реалізована після усунення помилки",
  "blocked_checks": ["slide_number_range"]
}]
```

**Batch upload — partial success:**

```json
// POST /courses/{id}/nodes/{node_id}/slide-mapping
// 201 Created
{
  "created": 8,
  "failed": 2,
  "results": [
    {
      "index": 0,
      "status": "created",
      "mapping_id": "...",
      "validation_state": "validated",
      "warnings": []
    },
    {
      "index": 3,
      "status": "failed",
      "errors": [{
        "field": "slide_number",
        "message": "Слайд 42 не існує в 'basics.pdf' (всього 30 слайдів)",
        "hint": "Допустимий діапазон: 1–30. Надішліть повторно з правильним slide_number"
      }]
    },
    {
      "index": 5,
      "status": "created",
      "mapping_id": "...",
      "validation_state": "pending_validation",
      "blocking_factors": [{
        "type": "material_not_ready",
        "filename": "advanced.pdf",
        "message": "Презентація на обробці. Перевірка буде реалізована після обробки"
      }]
    }
  ],
  "hints": {
    "resubmit": "Виправте помилки в записах з status='failed' і надішліть лише їх повторно",
    "batch_size": "Рекомендований розмір batch: до 50 маппінгів. При великій кількості — розбийте на частини"
  }
}
```

---

## Цільовий API (після Sprint 2)

### Material Tree Management

```
POST   /api/v1/courses                                     → створити курс
GET    /api/v1/courses                                      → список курсів (pagination)
GET    /api/v1/courses/{course_id}                          → курс + дерево + статуси + fingerprints
DELETE /api/v1/courses/{course_id}                          → видалити курс (cascade)

POST   /api/v1/courses/{id}/nodes                           → створити root-вузол
POST   /api/v1/courses/{id}/nodes/{node_id}/children        → створити дочірній вузол
PATCH  /api/v1/courses/{id}/nodes/{node_id}                 → оновити (title, description, order, parent_id)
DELETE /api/v1/courses/{id}/nodes/{node_id}                  → видалити (cascade children + materials)

POST   /api/v1/courses/{id}/nodes/{node_id}/materials       → додати матеріал (file або URL)
DELETE /api/v1/courses/{id}/materials/{material_id}          → видалити матеріал
POST   /api/v1/courses/{id}/materials/{material_id}/retry    → повторити ingestion
```

### Slide-Video Mapping

```
POST   /api/v1/courses/{id}/nodes/{node_id}/slide-mapping   → batch create (partial success)
GET    /api/v1/courses/{id}/nodes/{node_id}/slide-mapping    → list mappings for node
DELETE /api/v1/courses/{id}/slide-mapping/{mapping_id}       → видалити маппінг
```

### Structure Generation (per-node, каскадна)

```
POST   /api/v1/courses/{id}/structure/generate              → trigger для всього курсу
POST   /api/v1/courses/{id}/nodes/{node_id}/structure/generate → trigger для піддерева

         body: { "mode": "free" | "guided" }

         ← 200 OK:       snapshot з таким fingerprint+mode вже існує (idempotent)
         ← 202 Accepted: job створено
         ← 409 Conflict:  active job перетинається з запитаним scope
         ← 422 Unprocessable: немає жодного READY матеріалу в scope

GET    /api/v1/courses/{id}/structure                       → останній snapshot (course-level)
GET    /api/v1/courses/{id}/nodes/{node_id}/structure        → останній snapshot (node-level)

GET    /api/v1/courses/{id}/structure/jobs                   → всі generation jobs для курсу
GET    /api/v1/courses/{id}/nodes/{node_id}/structure/jobs   → jobs для конкретного scope
```

### Jobs (generic)

```
GET    /api/v1/jobs/{job_id}                                → статус будь-якого job-а
```

### Reports & Health

```
GET    /api/v1/reports/cost                                 → LLM cost report
GET    /health                                              → deep health (DB + S3 + Redis)
```

---

## Database Changes

### Нові таблиці

```sql
-- ── Material Tree ──
CREATE TABLE material_nodes (
    id UUID PRIMARY KEY DEFAULT uuid7(),
    course_id UUID NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    parent_id UUID REFERENCES material_nodes(id) ON DELETE CASCADE,
    title VARCHAR(500) NOT NULL,
    description TEXT,
    "order" INTEGER NOT NULL DEFAULT 0,
    node_fingerprint VARCHAR(64),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ix_material_nodes_course ON material_nodes(course_id);
CREATE INDEX ix_material_nodes_parent ON material_nodes(parent_id);

-- ── Material Entries (замість source_materials) ──
CREATE TABLE material_entries (
    id UUID PRIMARY KEY DEFAULT uuid7(),
    node_id UUID NOT NULL REFERENCES material_nodes(id) ON DELETE CASCADE,
    source_type source_type_enum NOT NULL,
    "order" INTEGER NOT NULL DEFAULT 0,
    source_url VARCHAR(2000) NOT NULL,
    filename VARCHAR(500),
    raw_hash VARCHAR(64),
    raw_size_bytes INTEGER,
    processed_hash VARCHAR(64),
    processed_content TEXT,
    processed_at TIMESTAMPTZ,
    pending_job_id UUID REFERENCES jobs(id) ON DELETE SET NULL,
    pending_since TIMESTAMPTZ,
    content_fingerprint VARCHAR(64),
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ix_material_entries_node ON material_entries(node_id);
CREATE INDEX ix_material_entries_pending_job ON material_entries(pending_job_id) WHERE pending_job_id IS NOT NULL;

-- ── Job Tracking ──
CREATE TABLE jobs (
    id UUID PRIMARY KEY DEFAULT uuid7(),
    course_id UUID REFERENCES courses(id) ON DELETE SET NULL,
    node_id UUID REFERENCES material_nodes(id) ON DELETE SET NULL,
    job_type VARCHAR(50) NOT NULL,
    priority VARCHAR(20) NOT NULL DEFAULT 'normal',
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    arq_job_id VARCHAR(100),
    input_params JSONB,
    result_material_id UUID REFERENCES material_entries(id) ON DELETE SET NULL,
    result_snapshot_id UUID REFERENCES course_structure_snapshots(id) ON DELETE SET NULL,
    depends_on JSONB,
    CONSTRAINT chk_job_result_exclusive CHECK (
        NOT (result_material_id IS NOT NULL AND result_snapshot_id IS NOT NULL)
    ),
    error_message TEXT,
    queued_at TIMESTAMPTZ DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    estimated_at TIMESTAMPTZ
);
CREATE INDEX ix_jobs_course ON jobs(course_id);
CREATE INDEX ix_jobs_node ON jobs(node_id);
CREATE INDEX ix_jobs_status ON jobs(status);

-- ── Slide-Video Mapping (redesigned) ──
-- Replaces old slide_video_mappings table
CREATE TABLE slide_video_mappings (
    id UUID PRIMARY KEY DEFAULT uuid7(),
    node_id UUID NOT NULL REFERENCES material_nodes(id) ON DELETE CASCADE,
    presentation_entry_id UUID NOT NULL REFERENCES material_entries(id) ON DELETE CASCADE,
    video_entry_id UUID NOT NULL REFERENCES material_entries(id) ON DELETE CASCADE,
    slide_number INTEGER NOT NULL,
    video_timecode_start VARCHAR(20) NOT NULL,
    video_timecode_end VARCHAR(20),
    "order" INTEGER NOT NULL DEFAULT 0,
    validation_state VARCHAR(30) NOT NULL DEFAULT 'pending_validation',
    blocking_factors JSONB,
    validation_errors JSONB,
    validated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ix_svm_node ON slide_video_mappings(node_id);
CREATE INDEX ix_svm_presentation ON slide_video_mappings(presentation_entry_id);
CREATE INDEX ix_svm_video ON slide_video_mappings(video_entry_id);
CREATE INDEX ix_svm_validation ON slide_video_mappings(validation_state) WHERE validation_state != 'validated';

-- ── Course Structure Snapshots ──
CREATE TABLE course_structure_snapshots (
    id UUID PRIMARY KEY DEFAULT uuid7(),
    course_id UUID NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    node_id UUID REFERENCES material_nodes(id) ON DELETE CASCADE,
    node_fingerprint VARCHAR(64) NOT NULL,
    mode VARCHAR(20) NOT NULL,
    structure JSONB NOT NULL,
    prompt_version VARCHAR(50),
    model_id VARCHAR(100),
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd FLOAT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ix_snapshots_course ON course_structure_snapshots(course_id);
CREATE INDEX ix_snapshots_node ON course_structure_snapshots(node_id);
CREATE UNIQUE INDEX uq_snapshots_identity
    ON course_structure_snapshots(course_id, COALESCE(node_id, '00000000-0000-0000-0000-000000000000'), node_fingerprint, mode);
```

### Міграція існуючих даних

```sql
-- 0. ALTER TABLE modules ADD COLUMN snapshot_id UUID REFERENCES course_structure_snapshots(id) ON DELETE SET NULL
-- 1. Для кожного Course створити root MaterialNode
-- 2. Перенести source_materials → material_entries (через root node)
-- 3. Перенести slide_video_mappings → нова структура
-- 4. Cleanup старих таблиць після верифікації
```

---

## Епіки та задачі

### Epic 0: Project Documentation Infrastructure (1-2 дні)

**Ціль:** Документація проєкту на GitHub Pages (mkdocs). ERD що оновлюється, структуровані описи спрінтів. Виконується **першим** — всі наступні епіки документуються вже в цій системі.

| # | Задача | Оцінка | Деталі |
|---|---|---|---|
| S2-000a | mkdocs setup + theme | 2h | mkdocs-material, pyproject інтеграція, `docs/` структура, nav config |
| S2-000b | GitHub Actions → GitHub Pages deploy | 2h | `mkdocs gh-deploy` через Actions on push to main |
| S2-000c | ERD page — Mermaid rendering | 2h | Mermaid plugin для mkdocs, ERD v4 як live-page |
| S2-000d | Sprint 1 — ретроспективний опис | 3h | `docs/sprints/sprint-1/index.md` — цілі, результати, архітектура |
| S2-000e | Sprint 2 — поточний опис | 2h | `docs/sprints/sprint-2/index.md` — цілі, AR-и, епіки |
| S2-000f | Структура документації + landing | 2h | Overview, Architecture (ERD), Sprints, API Reference |
| S2-000g | README оновлення | 1h | Посилання на docs site, badge |

**Структура `docs/`:**

```
docs/
├── index.md                          ← Overview проєкту
├── architecture/
│   ├── erd.md                        ← ERD (Mermaid, оновлюється щоспрінт)
│   ├── decisions.md                  ← Architecture Decision Records
│   └── infrastructure.md             ← Docker, deploy, env
├── sprints/
│   ├── index.md                      ← Sprint roadmap / timeline
│   ├── sprint-1/
│   │   ├── index.md                  ← Цілі, результати, метрики
│   │   └── review.md                 ← Ретроспектива, lessons learned
│   └── sprint-2/
│       ├── index.md                  ← Цілі, AR-и, scope
│       ├── epics.md                  ← Епіки з задачами і статусами
│       └── tasks/                    ← Окремі сторінки для складних задач (опціонально)
├── api/
│   ├── reference.md                  ← OpenAPI-based reference
│   ├── flow-guide.md                 ← User flow від створення курсу до результату
│   └── auth.md                       ← API keys, scopes, rate limits
└── development/
    ├── setup.md                      ← Local dev environment
    ├── testing.md                    ← Test strategy, running tests
    └── conventions.md                ← Code style, naming, PR process
```

### Epic 1: Infrastructure — ARQ + Redis (4-5 днів)

**Ціль:** Task queue з persistence, concurrency control, work window, job tracking, estimates.

| # | Задача | Оцінка | Деталі |
|---|---|---|---|
| S2-001 | Redis в docker-compose (dev + prod) | 2h | redis:7-alpine, appendonly yes, healthcheck |
| S2-002 | ARQ worker setup + Settings | 4h | `WorkerSettings`, connection pool, graceful shutdown |
| S2-003 | Worker config через env | 2h | max_jobs, timeout, max_tries в Settings |
| S2-004 | Work Window service | 4h | `WorkWindow` class, is_active_now(), next_start(), remaining_today() |
| S2-005 | Job priorities (IMMEDIATE/NORMAL) | 2h | Heavy jobs чекають вікна, light — ні |
| S2-006 | Job ORM model + repository | 3h | Jobs table, `JobRepository` CRUD, status transitions |
| S2-007 | Queue estimate service | 4h | Position, avg duration, window-aware estimated_start/complete |
| S2-008 | Замінити `BackgroundTasks` → ARQ enqueue | 3h | `ingest_material` як ARQ function, pending receipt |
| S2-009 | Ingestion completion callback | 3h | On complete: update MaterialEntry, invalidate fps, trigger revalidation |
| S2-010 | Job status API endpoint | 2h | `GET /jobs/{id}` |
| S2-011 | Health check — додати Redis | 1h | `/health` перевіряє Redis connectivity |
| S2-012 | Worker integration tests | 4h | Job lifecycle, window scheduling, retry, depends_on, callback |

### Epic 2: MaterialTree + MaterialEntry (4-5 днів)

**Ціль:** Recursive tree вузлів, MaterialEntry з raw/processed розділенням і квитанцією.

| # | Задача | Оцінка | Деталі |
|---|---|---|---|
| S2-013 | MaterialNode ORM model | 2h | Self-referential FK, node_fingerprint, relationships |
| S2-014 | MaterialEntry ORM model | 3h | Raw/processed layers, pending receipt, content_fingerprint |
| S2-015 | MaterialState derived property | 1h | RAW/PENDING/READY/INTEGRITY_BROKEN/ERROR logic |
| S2-016 | MaterialNode repository | 4h | CRUD, reorder, move (change parent), recursive fetch |
| S2-017 | MaterialEntry repository | 4h | CRUD, update_content (invalidates hash), pending receipt mgmt |
| S2-018 | Alembic migration: new tables + data migration | 4h | material_nodes, material_entries, migrate source_materials |
| S2-019 | Tree API endpoints (nodes) | 4h | POST/PATCH/DELETE nodes, nested children |
| S2-020 | Materials endpoint refactor | 3h | POST /nodes/{node_id}/materials, DELETE, retry |
| S2-021 | Course detail response — tree structure | 3h | GET /courses/{id} з повним деревом, станами, fingerprints |
| S2-022 | List courses endpoint | 1h | GET /api/v1/courses з pagination |
| S2-023 | Tree + MaterialEntry unit tests | 5h | CRUD, move, cascade, states, deep nesting |

### Epic 3: Merkle Fingerprints (2-3 дні)

**Ціль:** Lazy cached fingerprints з каскадною інвалідацією.

| # | Задача | Оцінка | Деталі |
|---|---|---|---|
| S2-024 | FingerprintService — material level | 2h | ensure_material_fp, invalidation |
| S2-025 | FingerprintService — node level (recursive) | 3h | ensure_node_fp, Merkle hash від children + materials |
| S2-026 | FingerprintService — course level | 1h | hash від root nodes |
| S2-027 | Cascade invalidation (_invalidate_up) | 2h | Будь-яка модифікація → скидає fp до кореня |
| S2-028 | Integration з MaterialEntry/Node repositories | 2h | Auto-invalidation при CRUD operations |
| S2-029 | Fingerprint в API responses | 2h | Всі GET endpoints повертають fingerprints |
| S2-030 | Fingerprint unit tests | 3h | Merkle correctness, invalidation cascade, lazy calculation |

### Epic 4: Heavy Steps Extraction (2-3 дні)

**Ціль:** Injectable heavy operations, serverless-ready boundary.

| # | Задача | Оцінка | Деталі |
|---|---|---|---|
| S2-031 | Heavy step protocols + param/result models | 2h | TranscribeFunc, DescribeSlidesFunc, ParsePDFFunc |
| S2-032 | Extract whisper transcription | 3h | Виділити з VideoProcessor, injectable |
| S2-033 | Extract vision/slide description | 3h | Виділити з PresentationProcessor |
| S2-034 | Extract web scraping | 2h | Виділити з WebProcessor |
| S2-035 | Refactor processors as orchestrators | 4h | Приймають heavy steps через DI |
| S2-036 | Factory for heavy steps | 2h | `create_heavy_steps(settings)` → local implementations |
| S2-037 | Heavy steps unit tests | 3h | Mock boundary, test orchestration |

### Epic 5: SlideVideoMapping — Redesign (3-4 дні)

**Ціль:** Explicit presentation↔video references, трирівнева валідація, deferred validation з auto-revalidation.

| # | Задача | Оцінка | Деталі |
|---|---|---|---|
| S2-038 | SlideVideoMapping ORM redesign | 3h | FK → MaterialEntry, validation fields, blocking_factors JSONB |
| S2-039 | MappingValidationService — structural validation (Level 1) | 3h | Node membership, source_type check, timecode format |
| S2-040 | MappingValidationService — content validation (Level 2) | 3h | Slide count range, timecode range (when READY) |
| S2-041 | MappingValidationService — deferred validation (Level 3) | 4h | Blocking factors, PENDING_VALIDATION state, JSONB structure |
| S2-042 | Auto-revalidation on ingestion complete | 3h | Hook into ingestion callback (S2-009), find_blocked_by, revalidate |
| S2-043 | Batch create endpoint (partial success) | 4h | Per-item results, errors with hints, resubmit guidance |
| S2-044 | Mapping CRUD endpoints | 2h | GET list, DELETE |
| S2-045 | SlideVideoMapping migration | 2h | Old table → new structure, data migration |
| S2-046 | Mapping validation unit tests | 4h | All 3 levels, auto-revalidation lifecycle, partial success |

### Epic 6: Structure Generation Pipeline (3-4 дні)

**Ціль:** Per-node trigger, каскадна обробка, fingerprint check, snapshot, conflict detection.

| # | Задача | Оцінка | Деталі |
|---|---|---|---|
| S2-047 | CourseStructureSnapshot ORM + repository | 3h | CRUD, query by (node_id, fingerprint, mode), unique constraint |
| S2-048 | Subtree readiness check | 2h | Знайти stale materials (RAW, INTEGRITY_BROKEN) в піддереві |
| S2-049 | Conflict detection (subtree overlap) | 3h | is_ancestor_or_same, active job overlap check |
| S2-050 | Generate structure ARQ task | 4h | Merge → ArchitectAgent → save snapshot |
| S2-051 | Cascade generation orchestrator | 4h | Ingestion jobs → depends_on → structure generation job |
| S2-052 | Free vs Guided mode | 3h | Різні prompt templates, mode parameter |
| S2-053 | Structure generation API | 4h | POST trigger (200/202/409/422), GET status, GET result |
| S2-054 | MergeStep refactor — tree-aware | 3h | Merge враховує ієрархію MaterialNode |
| S2-055 | Mapping warnings in generation | 2h | pending_validation/validation_failed маппінги → warnings |
| S2-056 | Structure generation tests | 4h | Pipeline mock, idempotency, conflicts, readiness, cascade |

### Epic 7: Integration Documentation (1-2 дні)

**Ціль:** Зовнішня команда може почати інтеграцію. Публікується на docs site (Epic 0).

| # | Задача | Оцінка | Деталі |
|---|---|---|---|
| S2-057 | Flow Guide | 3h | `docs/api/flow-guide.md` — повний сценарій від створення курсу до структури |
| S2-058 | API Reference update | 2h | `docs/api/reference.md` — OpenAPI schema, приклади запитів/відповідей |
| S2-059 | Auth & onboarding guide | 1h | `docs/api/auth.md` — ключ, scopes, rate limits |
| S2-060 | Error handling guide | 2h | `docs/api/errors.md` — коди помилок, retry стратегії, polling patterns |

---

## Залежності між епіками

```
Epic 0 (Docs Infrastructure) ──── ПЕРШИМ ──────────────────────────────┐
                                                                        │
Epic 1 (Queue) ──────────────────────────────────┐                     │
                                                   │                     │
Epic 2 (MaterialTree + Entry) ───────────────────┤                     │
                                                   │                     │
Epic 3 (Fingerprints) ──── Epic 2 ───────────────┤                     │
                                                   ├──→ Epic 6 (Structure Gen) │
Epic 4 (Heavy Steps) ──── Epic 1 ───────────────┤     │               │
                                                   │     └──→ Epic 7 (Docs) ──┘
Epic 5 (SlideVideoMapping) ──── Epic 1 + Epic 2 ─┘          ↑ updates docs site
```

**Рекомендований порядок:**
0. **Epic 0 (Docs Infrastructure)** — першим, 1-2 дні. Далі всі результати документуються в mkdocs
1. **Epic 1 (Queue)** + **Epic 2 (MaterialTree)** — паралельно, розблоковують все інше
2. **Epic 3 (Fingerprints)** — після Epic 2
3. **Epic 4 (Heavy Steps)** — паралельно з Epic 3
4. **Epic 5 (SlideVideoMapping)** — після Epic 1 + Epic 2 (потребує MaterialEntry + ingestion callback)
5. **Epic 6 (Structure Generation)** — після всіх попередніх
6. **Epic 7 (Integration Documentation)** — публікується на docs site (Epic 0), паралельно з Epic 6

---

## Нові залежності (pyproject.toml)

```toml
[project]
dependencies = [
    # ... existing ...
    "arq>=0.26",          # task queue
    "redis[hiredis]>=5",  # ARQ backend + fast connection
]

[project.optional-dependencies]
docs = [
    "mkdocs-material>=9",          # theme
    "mkdocs-mermaid2-plugin>=1",   # ERD rendering
]
```

---

## Docker Compose зміни

```yaml
# docker-compose.prod.yaml — додаються:
redis:
  image: redis:7-alpine
  container_name: course-supporter-redis
  restart: unless-stopped
  volumes:
    - redis-data:/data
  command: redis-server --appendonly yes
  healthcheck:
    test: ["CMD", "redis-cli", "ping"]
    interval: 10s
    timeout: 5s
    retries: 5
  networks:
    - default

worker:
  build: .
  container_name: course-supporter-worker
  restart: unless-stopped
  env_file: .env.prod
  command: arq course_supporter.worker.WorkerSettings
  depends_on:
    redis: { condition: service_healthy }
    postgres-cs: { condition: service_healthy }
  networks:
    - default

volumes:
  redis-data:
  # ... existing volumes ...
```

---

## Definition of Done

- [ ] **Docs site live** на GitHub Pages (публічний)
- [ ] mkdocs з ERD (Mermaid), Sprint 1 ретроспектива, Sprint 2 опис
- [ ] Auto-deploy docs on push to main (GitHub Actions)
- [ ] Redis + ARQ worker в docker-compose (dev і prod)
- [ ] Всі background tasks через ARQ (жоден `BackgroundTasks`)
- [ ] Work window: конфігурація через env, heavy jobs чекають вікна
- [ ] Job tracking: status, estimated_at, priorities, depends_on
- [ ] Ingestion callback: update entry, invalidate fps, trigger mapping revalidation
- [ ] MaterialTree: recursive nodes, матеріали на будь-якому рівні
- [ ] MaterialEntry: raw/processed separation, pending receipt, derived state
- [ ] Merkle fingerprints: material → node → course, lazy cached, cascade invalidation
- [ ] SlideVideoMapping: explicit FK на presentation + video MaterialEntry
- [ ] Mapping validation: 3 levels, deferred validation, auto-revalidation
- [ ] Batch mapping upload: partial success, per-item results, hints
- [ ] Structure generation: per-node trigger, cascade ingestion+generation
- [ ] Conflict detection: subtree overlap → 409
- [ ] Idempotency: same fingerprint+mode → 200 з existing snapshot
- [ ] Free/Guided modes з різними prompt templates
- [ ] Heavy steps виділені як injectable callables (serverless-ready)
- [ ] Processors — оркестратори з DI
- [ ] Flow Guide документація для зовнішньої команди
- [ ] Alembic міграції з data migration (forward + downgrade)
- [ ] `make check` зелений (ruff + mypy + pytest)
- [ ] Tenant isolation перевірена для всіх нових endpoints

---

## Не входить (backlog)

- Exercise/Assessment endpoints (Sprint 3)
- Student progress tracking (Sprint 3)
- Фактична Lambda/serverless міграція heavy steps
- Автоматичний SlideVideoMapping (vision matching)
- RAG / vector search по concepts
- Integration tests з реальним Redis/PostgreSQL
- Управління користувачами через API (tenant CRUD)
