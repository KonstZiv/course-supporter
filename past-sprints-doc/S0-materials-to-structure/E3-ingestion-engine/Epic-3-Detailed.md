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

1. **SourceProcessor ABC + Pydantic schemas** ([S1-011](T1-source-processor/T011-source-processor.md)) — базовий інтерфейс та моделі даних:
   - `SourceProcessor` ABC в `ingestion/base.py`: `async def process(source: SourceMaterial, *, router: ModelRouter | None = None) -> SourceDocument`
   - `ProcessingError`, `UnsupportedFormatError` — custom exceptions
   - Pydantic-моделі в `models/source.py`: `ChunkType` (StrEnum, 7 типів), `ContentChunk` (type, text, index, metadata dict), `SourceDocument`
   - Pydantic-моделі в `models/course.py`: `SlideVideoMapEntry`, `CourseContext` (list of SourceDocument + mappings)
2. **VideoProcessor primary** ([S1-012](T2-video-primary/T012-video-primary.md)) — `GeminiVideoProcessor`: upload відео через Gemini File API → `router.complete(action="video_analysis")` → parse timestamped transcript → `ContentChunk(TRANSCRIPT)`. `VideoProcessor` — composition shell з fallback placeholder.
3. **VideoProcessor fallback** ([S1-013](T3-video-fallback/T013-video-fallback.md)) — `WhisperVideoProcessor`: FFmpeg subprocess → audio extraction → Whisper transcribe (в thread pool) → segments → `ContentChunk(TRANSCRIPT)`. Підключення до `VideoProcessor` як автоматичний fallback при збої Gemini.
4. **PresentationProcessor** ([S1-014](T4-presentation/T014-presentation.md)) — PDF (`fitz.open()` → page text + optional pixmap → Vision LLM) та PPTX (`Presentation()` → shapes text). Vision LLM через `router.complete(action="presentation_analysis")` для `SLIDE_DESCRIPTION` chunks. Graceful degradation при збої LLM.
5. **TextProcessor** ([S1-015](T5-text/T015-text.md)) — MD (regex heading parse), DOCX (`docx.Document` heading styles), HTML (`BeautifulSoup` h1..h6 + p), TXT → `HEADING` + `PARAGRAPH` chunks. НЕ використовує LLM.
6. **WebProcessor** ([S1-016](T6-web/T016-web.md)) — `trafilatura.fetch_url()` + `trafilatura.extract()` → `WEB_CONTENT` chunks. Raw HTML → `metadata["content_snapshot"]`. Domain + fetched_at у metadata. НЕ використовує LLM.
7. **MergeStep** ([S1-017](T7-merge/T017-merge.md)) — синхронний (не async). Сортування documents за пріоритетом (video→presentation→text→web). Cross-references: slide SLIDE_TEXT chunks збагачуються `video_timecode` з `SlideVideoMapEntry` mappings.
8. **SourceMaterial persistence** ([S1-018](T8-source-persistence/T018-source-persistence.md)) — `SourceMaterialRepository` з CRUD (create, get_by_id, get_by_course_id, update_status, delete). Status machine з валідацією переходів. `flush()` замість `commit()` — caller контролює transaction.

## Для чого

Ingestion — це "вхідна воронка" системи. Якість роботи Architect Agent (Epic 4) напряму залежить від якості витягнутого контенту. Кожен процесор оптимізований для свого типу матеріалу, а MergeStep створює повний контекст із cross-references між відео-таймкодами, слайдами та текстом.

## Контрольні точки

- [x] `SourceProcessor` ABC визначає контракт для всіх процесорів
- [x] `SourceDocument` та `ContentChunk` Pydantic-моделі валідують output процесорів
- [x] VideoProcessor (primary): відео → таймкодований транскрипт через ModelRouter (`video_analysis` action)
- [x] VideoProcessor (fallback): те саме через FFmpeg + Whisper при помилці Gemini
- [x] PresentationProcessor: PDF/PPTX → текст + slide images → ModelRouter (`presentation_analysis` action) для діаграм
- [x] TextProcessor: MD/DOCX/HTML → chunked plain text (без LLM)
- [x] WebProcessor: URL → extracted content + snapshot (без LLM)
- [x] MergeStep: кілька SourceDocument → CourseContext з cross-references
- [x] SourceMaterial repository: CRUD + статус-машина працює коректно
- [x] Unit-тести з mocked LLM responses — **101 тест** (11+17+11+13+11+8+13+17)
- [x] `make check` проходить

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
| ID | Назва | Статус | Тести | Примітка |
|:---|:---|:---|:---|:---|
| S1-011 | SourceProcessor інтерфейс | ✅ | 11 | [spec](T1-source-processor/T011-source-processor.md) · [issue](T1-source-processor/T011-github-issue.md) — ABC + schemas |
| S1-012 | VideoProcessor (primary) | ✅ | 17 | [spec](T2-video-primary/T012-video-primary.md) · [issue](T2-video-primary/T012-github-issue.md) — GeminiVideoProcessor + VideoProcessor shell |
| S1-013 | VideoProcessor (fallback) | ✅ | 11 | [spec](T3-video-fallback/T013-video-fallback.md) · [issue](T3-video-fallback/T013-github-issue.md) — WhisperVideoProcessor + fallback |
| S1-014 | PresentationProcessor | ✅ | 13 | [spec](T4-presentation/T014-presentation.md) · [issue](T4-presentation/T014-github-issue.md) — PDF + PPTX + Vision LLM |
| S1-015 | TextProcessor | ✅ | 11 | [spec](T5-text/T015-text.md) · [issue](T5-text/T015-github-issue.md) — MD/DOCX/HTML/TXT, без LLM |
| S1-016 | WebProcessor | ✅ | 8 | [spec](T6-web/T016-web.md) · [issue](T6-web/T016-github-issue.md) — trafilatura, без LLM |
| S1-017 | MergeStep | ✅ | 13 | [spec](T7-merge/T017-merge.md) · [issue](T7-merge/T017-github-issue.md) — sync merge + cross-references |
| S1-018 | SourceMaterial persistence | ✅ | 17 | [spec](T8-source-persistence/T018-source-persistence.md) · [issue](T8-source-persistence/T018-github-issue.md) — Repository + status machine |

**Загалом: 101 тест, Epic 3 DONE**

## Ризики

- **Gemini File API** нестабільний для великих відео → fallback готовий (S1-013)
- **PPTX з нестандартним форматуванням** → фокус на стандартних, edge cases в backlog
- **Web scraping** може блокуватись сайтами → trafilatura як найнадійніший варіант, graceful degradation при помилці
- **FFmpeg system dependency** → в CI mock, локально вимагає `brew install ffmpeg` / `apt install ffmpeg`
- **Whisper + PyTorch ~2GB** → optional `media` group, не встановлюється в CI

---

## Прийняті архітектурні рішення

### 1. Сигнатура SourceProcessor.process() ✅

```python
async def process(source: SourceMaterial, *, router: ModelRouter | None = None) -> SourceDocument
```

Процесор отримує ORM-об'єкт `SourceMaterial` і keyword-only `router`. TextProcessor/WebProcessor ігнорують router. VideoProcessor/PresentationProcessor вимагають router для LLM-викликів.

### 2. VideoProcessor: Composition ✅

Обрано **варіант B** — два окремі класи:
- `GeminiVideoProcessor(SourceProcessor)` — upload + Gemini Vision
- `WhisperVideoProcessor(SourceProcessor)` — FFmpeg + Whisper

`VideoProcessor(SourceProcessor)` — composition shell:
```python
def __init__(self, *, enable_whisper: bool = True):
    self._gemini = GeminiVideoProcessor()
    self._whisper = WhisperVideoProcessor() if enable_whisper else None
```
Fallback: Gemini fails → Whisper; обидва fail → raise останню помилку.

### 3. MinIO: після MVP ✅

Процесори працюють з `source_url` напряму (local path або remote URL). S3/MinIO інтеграція — окремий task після Epic 3. ORM `source_url: String(2000)` підтримує і local paths, і S3 URLs.

### 4. Тестування ✅

Unit-тести мокають `ModelRouter`, SDK calls (fitz, pptx, docx, trafilatura, whisper), FFmpeg subprocess. Інтеграційні тести з реальними API — `tests/evals/`. Фактична кількість тестів: **101** (11+17+11+13+11+8+13+17).

### 5. Custom exceptions ✅

- `ProcessingError(Exception)` — загальна помилка обробки
- `UnsupportedFormatError(ProcessingError)` — непідтримуваний формат

Всі процесори raise `UnsupportedFormatError` при невалідному `source_type` або розширенні.

---

## Shared Pydantic Models (S1-011)

Ці моделі використовуються всіма задачами Epic 3:

```python
# models/source.py
class ChunkType(StrEnum):
    TRANSCRIPT = "transcript"           # video timecoded text
    SLIDE_TEXT = "slide_text"           # extracted text from slide
    SLIDE_DESCRIPTION = "slide_description"  # vision LLM analysis
    PARAGRAPH = "paragraph"             # text document paragraph
    HEADING = "heading"                 # text document heading
    WEB_CONTENT = "web_content"         # extracted web content
    METADATA = "metadata"              # general metadata

class ContentChunk(BaseModel):
    chunk_type: ChunkType
    text: str
    index: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

class SourceDocument(BaseModel):
    source_type: str  # "video" | "presentation" | "text" | "web"
    source_url: str
    title: str = ""
    chunks: list[ContentChunk] = Field(default_factory=list)
    processed_at: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = Field(default_factory=dict)

# models/course.py
class SlideVideoMapEntry(BaseModel):
    slide_number: int
    video_timecode: str  # "01:23:45", matches ORM String(20)

class CourseContext(BaseModel):
    documents: list[SourceDocument]
    slide_video_mappings: list[SlideVideoMapEntry] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
```

---

## Task Specs

Детальні специфікації для кожної задачі:

```
E3-ingestion-engine/
├── T1-source-processor/     S1-011: ABC + schemas
├── T2-video-primary/        S1-012: GeminiVideoProcessor
├── T3-video-fallback/       S1-013: WhisperVideoProcessor
├── T4-presentation/         S1-014: PDF + PPTX
├── T5-text/                 S1-015: MD/DOCX/HTML/TXT
├── T6-web/                  S1-016: trafilatura
├── T7-merge/                S1-017: MergeStep
└── T8-source-persistence/   S1-018: Repository
```

Кожна папка містить `T0XX-*.md` (повна spec з кодом і тестами) + `T0XX-github-issue.md` (summary).
