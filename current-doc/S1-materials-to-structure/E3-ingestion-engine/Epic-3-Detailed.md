# Epic 3: Ingestion Engine

## Мета

Обробка всіх типів матеріалів курсу. Після цього епіку — система може прийняти відео, PDF/PPTX, текст (MD/DOCX/HTML) або URL і перетворити кожне джерело на уніфікований `SourceDocument`. Кілька джерел об'єднуються в `CourseContext`, готовий для Architect Agent.

## Передумови (виконано)

- **Epic 1 ✅**: DB schema готова — таблиця `source_materials` з enum `source_type_enum` (video, presentation, text, web), статус-машина `processing_status_enum` (pending, processing, done, error), `content_snapshot`, `error_message`.
- **Epic 2 ✅**: `ModelRouter` готовий. Actions `video_analysis` (requires: vision, long_context) та `presentation_analysis` (requires: vision, structured_output) вже в `config/models.yaml`. Провайдери: Gemini (vision), GPT-4o-mini (vision), Claude (no vision), DeepSeek (no vision).
- **Existing stubs**: `ingestion/base.py`, `ingestion/video.py`, `ingestion/presentation.py`, `ingestion/text.py`, `ingestion/web.py`, `ingestion/merge.py` — порожні файли з TODO. `models/source.py`, `models/course.py` — порожні. `storage/repositories.py` — порожній. `storage/database.py` — готовий (`async_session`, `get_session()`).
- **Dependencies** (вже в `pyproject.toml`): python-pptx, pymupdf, python-docx, trafilatura, beautifulsoup4. Optional: openai-whisper (media group).
- **System dependency**: FFmpeg — потрібен для S1-013 (Whisper fallback). НЕ Python package, встановлюється окремо. В CI — mock.

## Що робимо

Чотири процесори + merge + persistence:

1. **SourceProcessor ABC + Pydantic schemas** (S1-011) — базовий інтерфейс та моделі даних:
   - `SourceProcessor` ABC в `ingestion/base.py`: `async def process(source: SourceMaterial, router: ModelRouter) -> SourceDocument`
   - Pydantic-моделі в `models/source.py`: `SourceDocument`, `ContentChunk` (type, text, metadata dict)
   - Pydantic-моделі в `models/course.py`: `CourseContext` (list of SourceDocument + SlideVideoMapping)
2. **VideoProcessor primary** (S1-012) — Gemini Vision через ModelRouter (`action="video_analysis"`): завантаження відео через File API → таймкодований транскрипт зі structured output.
3. **VideoProcessor fallback** (S1-013) — FFmpeg → сегменти → Whisper v3 STT → об'єднання. Автоматичне переключення при помилці Gemini. Потребує FFmpeg (system) + openai-whisper (media group).
4. **PresentationProcessor** (S1-014) — PDF (PyMuPDF) та PPTX (python-pptx): витягування тексту, рендеринг слайдів у зображення, Vision LLM через ModelRouter (`action="presentation_analysis"`) для діаграм/графіків.
5. **TextProcessor** (S1-015) — MD (raw read), DOCX (python-docx), HTML (beautifulsoup4) → plain text з чанкуванням по структурі документа (заголовки, параграфи). НЕ використовує LLM.
6. **WebProcessor** (S1-016) — Fetch HTML → trafilatura → content extraction → snapshot збереження + URL preservation. НЕ використовує LLM.
7. **MergeStep** (S1-017) — об'єднання кількох `SourceDocument` у `CourseContext`. Інтеграція `SlideVideoMapping` (ручний маппінг слайдів до таймкодів).
8. **SourceMaterial persistence** (S1-018) — Repository pattern в `storage/repositories.py`: CRUD для `source_materials` ORM, статус-машина (pending → processing → done/error), збереження `content_snapshot`.

## Для чого

Ingestion — це "вхідна воронка" системи. Якість роботи Architect Agent (Epic 4) напряму залежить від якості витягнутого контенту. Кожен процесор оптимізований для свого типу матеріалу, а MergeStep створює повний контекст із cross-references між відео-таймкодами, слайдами та текстом.

## Контрольні точки

- [ ] `SourceProcessor` ABC визначає контракт для всіх процесорів
- [ ] `SourceDocument` та `ContentChunk` Pydantic-моделі валідують output процесорів
- [ ] VideoProcessor (primary): відео → таймкодований транскрипт через ModelRouter (`video_analysis` action)
- [ ] VideoProcessor (fallback): те саме через FFmpeg + Whisper при помилці Gemini
- [ ] PresentationProcessor: PDF/PPTX → текст + slide images → ModelRouter (`presentation_analysis` action) для діаграм
- [ ] TextProcessor: MD/DOCX/HTML → chunked plain text (без LLM)
- [ ] WebProcessor: URL → extracted content + snapshot (без LLM)
- [ ] MergeStep: кілька SourceDocument → CourseContext з cross-references
- [ ] SourceMaterial repository: CRUD + статус-машина працює коректно
- [ ] Unit-тести з mocked LLM responses
- [ ] `make check` проходить

## Залежності

- **Блокується:** Epic 1 (DB, config) ✅, Epic 2 (ModelRouter для Vision LLM) ✅
- **Блокує:** Epic 4 (ArchitectAgent потребує CourseContext)
- **Порядок імплементації:**
  1. S1-011 (інтерфейс + schemas) — блокує все інше
  2. S1-015 (TextProcessor) + S1-016 (WebProcessor) — найпростіші, без LLM
  3. S1-014 (PresentationProcessor) — Vision LLM
  4. S1-012 (VideoProcessor primary) + S1-013 (fallback) — найскладніші
  5. S1-017 (MergeStep) — потребує готових процесорів
  6. S1-018 (SourceMaterial persistence) — можна паралельно з процесорами

## Задачі

| ID | Назва | Естімейт | Примітка |
|:---|:---|:---|:---|
| S1-011 | SourceProcessor інтерфейс | 0.25 дня | ABC + Pydantic schemas (SourceDocument, ContentChunk, CourseContext) |
| S1-012 | VideoProcessor (primary) | 0.5 дня | Gemini Vision через ModelRouter |
| S1-013 | VideoProcessor (fallback) | 0.5 дня | FFmpeg + Whisper, system dep |
| S1-014 | PresentationProcessor | 0.5 дня | PDF + PPTX + Vision LLM |
| S1-015 | TextProcessor | 0.25 дня | Найпростіший, без LLM |
| S1-016 | WebProcessor | 0.25 дня | trafilatura, без LLM |
| S1-017 | MergeStep | 0.5 дня | cross-references logic |
| S1-018 | SourceMaterial persistence | 0.25 дня | Repository + status machine |

**Загалом: 3–4 дні** (найбільший епік спрінту)

## Ризики

- **Gemini File API** нестабільний для великих відео → fallback готовий (S1-013)
- **PPTX з нестандартним форматуванням** → фокус на стандартних, edge cases в backlog
- **Web scraping** може блокуватись сайтами → trafilatura як найнадійніший варіант, graceful degradation при помилці
- **FFmpeg system dependency** → в CI mock, локально вимагає `brew install ffmpeg` / `apt install ffmpeg`
- **Whisper + PyTorch ~2GB** → optional `media` group, не встановлюється в CI

---

## Пропозиції покращень (для обговорення)

### 1. Сигнатура SourceProcessor.process()

Документ пропонував `process(source_url) -> SourceDocument`. Але реально процесору потрібні:
- `source_url` (або path)
- `source_type` (для dispatch)
- `ModelRouter` (для Vision LLM)
- Можливо `MinIO client` (для збереження файлів)

**Пропозиція:** `async def process(source: SourceMaterial, *, router: ModelRouter | None = None) -> SourceDocument`. Процесор отримує ORM-об'єкт і вирішує що з ним робити. TextProcessor/WebProcessor ігнорують router.

### 2. VideoProcessor: два файли чи один?

S1-012 (primary) та S1-013 (fallback) — це окремі task, але це один `VideoProcessor` з двома стратегіями. Варіанти:
- **A) Один клас** `VideoProcessor` з `try: gemini_path() except: whisper_path()`
- **B) Два класи** `GeminiVideoProcessor` + `WhisperVideoProcessor`, композиція в `VideoProcessor`

Варіант B кращий для тестування і відповідальності.

### 3. MinIO integration

Документ не згадує де зберігаються оригінальні файли (відео, PDF). ORM має `source_url` (String 2000) — це може бути URL або S3 path. MinIO вже налаштований в Docker Compose. Потрібно визначити:
- Чи зберігає Ingestion файли в MinIO?
- Чи передає URL напряму провайдеру (Gemini File API)?

Рекомендація: на цьому етапі процесори працюють з `source_url` напряму (local path або remote URL). MinIO integration — після MVP.

### 4. Тестування з реальними API

Процесори використовують LLM (VideoProcessor, PresentationProcessor). Unit-тести мають мокати `ModelRouter` calls. Інтеграційні тести (з реальними API) — в `tests/evals/` (вже є stub).

### 5. Порядок імплементації

Рекомендований порядок (від простого до складного):
1. S1-011 → S1-018 (паралельно з S1-015)
2. S1-015 → S1-016
3. S1-014
4. S1-012 → S1-013
5. S1-017
