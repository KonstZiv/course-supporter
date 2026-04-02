# VD Implementation Plan (post-spike)

**Дата:** 2026-04-02
**Статус:** Draft — потребує review
**Базується на:** `VD-PIPELINE-PLAN.md` (original), `VD-SPIKE-REPORT.md` (spike findings)
**Замінює:** Фази 4-6 оригінального плану

---

## 1. Що змінилось після spike

| Аспект | Оригінальний план | Після spike |
|--------|-------------------|-------------|
| Stage B | Two-pass: classify → filter → detail | **Single-pass** + hierarchical memory |
| Stage C | Умовний OCR engine | **Скіпнуто** (lite 99.3%) |
| Модель | Chain: Flash → Pro/GPT-4o | **Єдина: gemini-3.1-flash-lite-preview** |
| Output format | JSON structured | **Markdown** (Scene Composition + Elements) |
| Frame selection | Pass 1 classification → skip talking_head | **Dense Coverage** (все + gap fill ≤15s) |
| Aggregation | Simple timestamp merge | **Hierarchical Memory** + **Cross-modal alignment** |
| external_services.yaml | 3 нових actions (classification, analysis, correction) | **1 action** (visual_eyes) |

---

## 2. Архітектура (оновлена)

```
Video file
│
├── Audio track → STT (ElevenLabs / GPT-4o Mini / Deepgram)
│   └── TranscriptResult (timestamped segments)
│
├── Video track → VD Pipeline (self-contained, нічого не знає про STT)
│   │
│   ├── Stage A: Frame Sampling (spike-proven)
│   │   fps=0.5 → dHash(16, 5%) dedup → PiP mask → gap fill (≤15s)
│   │   → SampledFrame[] + Scene[] boundaries
│   │
│   └── Stage B: Visual Analysis (spike-proven)
│       Eyes: per-frame Vision LLM (lite, prompt v3, 1-3 images)
│       → Instant Memory (code-based merge within scene)
│       → Scene Memory (per-scene synthesis)
│       → Course Memory (running context, feeds back to Eyes)
│       → VDResult (list[SceneAnalysis])
│
└── Cross-modal Alignment & Aggregation (ПІСЛЯ обох потоків)
    VideoProcessor або окремий AggregationStep:
    TranscriptResult + VDResult → temporal alignment
    → semantic cross-referencing → conflict detection
    → verification report → unified SourceDocument
```

**Ключовий принцип:** STT і VD — **повністю незалежні потоки**. VD Pipeline нічого не знає про STT. Cross-modal alignment — це окремий downstream етап на рівні `VideoProcessor` / `ingestion/`, а не частина VD module.

---

## 3. Cross-modal Alignment & Verification (ingestion level, НЕ частина VD)

> **Належить до:** `src/course_supporter/ingestion/alignment.py`
> **Виконується:** ПІСЛЯ завершення обох потоків (STT і VD)
> **Задача:** VD-007 (Фаза 5: Integration)

### 3.1. Проблема

Два незалежних потоки (STT audio text, VD visual text) описують той самий контент з різних кутів і з temporal offsets.

### 3.2. Сценарії неузгодженості

| Сценарій | Приклад | Як обробити |
|----------|---------|-------------|
| **STT leads** | Лектор: "зараз покажу помилку" → через 3с з'являється traceback | VD scene прив'язати до STT segment ±10s |
| **VD leads** | Новий слайд → через 2-3с лектор починає пояснювати | STT segment прив'язати до попередньої VD scene |
| **Contradiction** | STT: "fa12" vs VD: "faa12" | **VD wins** (візуальне = ground truth для коду) |
| **Silent visual** | Лектор мовчить, в terminal з'являється output | VD-only chunk, STT gap — OK |
| **Verbal-only** | Лектор пояснює без візуальних змін | STT-only chunk, VD talking_head — OK |
| **Forward reference** | "ми побачимо цей паттерн пізніше" | Metadata: forward_ref, не матчити |
| **Concurrent mismatch** | Лектор говорить про topic A, але ще показує slide B | Transition window — нормально для 2-5с |

### 3.3. Алгоритм alignment

```python
class CrossModalAligner:
    """Align STT transcript segments with VD scene analysis."""

    WINDOW_SEC: float = 10.0       # temporal match window
    OVERLAP_MIN_SEC: float = 2.0   # мінімальний overlap для match

    def align(
        self,
        stt: TranscriptResult,
        vd: list[SceneAnalysis],
    ) -> list[AlignedSegment]:
        """Three-phase alignment."""

        # Phase 1: Temporal alignment
        #   Кожна VD scene має [start_sec, end_sec]
        #   Знайти STT segments що перекриваються з VD scene ± WINDOW_SEC
        pairs = self._temporal_match(stt.segments, vd)

        # Phase 2: Semantic cross-reference
        #   Витягти keywords/identifiers з обох потоків
        #   Знайти семантичні зв'язки (STT mentions "function X" + VD shows "def X")
        self._semantic_link(pairs)

        # Phase 3: Conflict detection
        #   Порівняти extracted text з STT transcript
        #   Якщо conflict → flag + prefer VD for code, STT for natural language
        self._detect_conflicts(pairs)

        return pairs
```

### 3.4. Conflict resolution rules

| Content type | STT vs VD conflict | Winner | Reasoning |
|--------------|-------------------|--------|-----------|
| Code/identifiers | `fa12` vs `faa12` | **VD** | Екран = ground truth для коду |
| Numbers | "двісті" vs `200` | **VD** | Точніше |
| Natural language explanations | STT "пояснення X" vs VD "slide text Y" | **Both** | Доповнюють одне одного |
| Timing/sequence | STT "спочатку" vs VD shows later | **STT** | Лектор знає послідовність |

### 3.5. Verification checks

```python
class AlignmentVerifier:
    """Post-alignment quality checks."""

    def verify(self, aligned: list[AlignedSegment]) -> AlignmentReport:
        return AlignmentReport(
            # Coverage: чи є gaps >30s без жодного потоку?
            coverage_gaps=self._find_gaps(aligned, max_gap_sec=30),

            # Orphans: VD scenes без жодного STT match (OK для short scenes)
            vd_orphans=self._vd_without_stt(aligned),

            # Orphans: STT segments без VD context (OK для talking_head)
            stt_orphans=self._stt_without_vd(aligned),

            # Conflicts: де STT і VD суперечать
            conflicts=self._collect_conflicts(aligned),

            # Alignment confidence: % пар з semantic overlap
            semantic_coverage=self._semantic_coverage(aligned),
        )
```

### 3.6. Output: unified chunks

```python
class ChunkType(StrEnum):
    # Існуючі
    TRANSCRIPT = "transcript"
    SLIDE_TEXT = "slide_text"
    # ...

    # Нові для VD pipeline
    VISUAL_SCENE = "visual_scene"          # повний scene analysis (description + code)
    VISUAL_CODE = "visual_code"            # окремий code block з екрану
    VISUAL_SLIDE = "visual_slide"          # текст слайду
    VISUAL_TERMINAL = "visual_terminal"    # terminal output
    ALIGNED_SEGMENT = "aligned_segment"    # merged STT + VD segment
```

---

## 4. Модульна структура

```
src/course_supporter/vd/
├── __init__.py
├── schemas.py              # VD-001: Pydantic models
├── frame_sampler.py        # VD-002: Stage A
├── pip_tracker.py          # VD-002b: PiP detection (може бути частиною frame_sampler)
├── visual_analyzer.py      # VD-003: Eyes (per-frame Vision LLM)
├── memory_pipeline.py      # VD-004: instant → scene → course memory
├── pipeline.py             # VD-005: VDPipeline orchestrator
└── prompts/
    ├── eyes_v3.txt         # Prompt v3 template
    ├── instant_merge.txt   # Instant memory merge prompt
    ├── scene_memory.txt    # Scene synthesis prompt
    └── course_memory.txt   # Course running context prompt
```

---

## 5. Задачі implementation

### Фаза 4: VD Module (core)

#### VD-001: schemas.py — Pydantic models

Визначити всі data models на основі spike JSON структур:

```python
# Frame & Scene (Stage A output)
class SampledFrame(BaseModel):
    frame_id: str                    # "golden_042_1220s"
    filename: str                    # "golden_042_1220s.jpg"
    timestamp_sec: float
    scene_id: int
    dhash: str
    hamming_from_prev: int
    has_gt: bool = False             # є ground truth для eval
    is_gap_fill: bool = False        # додано gap fill

class Scene(BaseModel):
    scene_id: int
    frame_ids: list[str]
    start_sec: float
    end_sec: float

class FrameSamplingResult(BaseModel):
    frames: list[SampledFrame]
    scenes: list[Scene]
    pip_mask: tuple[int, int, int, int] | None   # (x1, y1, x2, y2)
    video_resolution: tuple[int, int]
    sampling_params: dict

# Eyes output (Stage B)
class EyesResult(BaseModel):
    frame_id: str
    scene_id: int
    response: str                    # raw Markdown response
    context_frames: list[str]        # frame_ids of context images sent
    model: str

# Memory pipeline output
class InstantMemory(BaseModel):
    scene_id: int
    merged_text: str                 # code-based or LLM merge of frame responses
    frame_count: int

class SceneMemory(BaseModel):
    scene_id: int
    scene_type: str                  # slide, code_editing, terminal, diagram, talking_head
    summary: str                     # 2-3 sentences, Ukrainian
    complete_text: str               # all extracted text/code
    topics: list[str]
    importance: int                  # 1-5

class CourseMemory(BaseModel):
    text: str                        # ≤200 words, running context
    scenes_covered: int

class SceneAnalysis(BaseModel):
    """Complete analysis of one scene — final output of Stage B."""
    scene: Scene
    eyes_results: list[EyesResult]
    instant_memory: InstantMemory
    scene_memory: SceneMemory

class VDResult(BaseModel):
    """Complete output of VD pipeline."""
    scenes: list[SceneAnalysis]
    course_memory: CourseMemory
    frames_total: int
    frames_analyzed: int
    model: str

# Cross-modal alignment (Stage D)
class AlignedSegment(BaseModel):
    start_sec: float
    end_sec: float
    stt_text: str | None              # що лектор каже
    vd_scene: SceneAnalysis | None    # що на екрані
    semantic_overlap: float           # 0.0-1.0, наскільки STT і VD про те саме
    conflicts: list[str]              # виявлені розбіжності
    alignment_confidence: float       # 0.0-1.0

class AlignmentReport(BaseModel):
    coverage_gaps: list[tuple[float, float]]    # (start, end) без контенту
    vd_orphans: list[int]                       # scene_ids без STT
    stt_orphans: list[tuple[float, float]]      # timestamp ranges без VD
    conflicts: list[dict]                       # {type, stt_value, vd_value, timestamp}
    semantic_coverage: float                    # % пар з semantic overlap >0.3
```

**Acceptance criteria:**
- Всі models мають валідацію і serialization
- mypy --strict проходить
- Відповідають реальним spike JSON структурам

---

#### VD-002: frame_sampler.py — Stage A

Port з `scripts/spike_frame_sampling.py`. Async interface.

```python
class FrameSampler:
    """Extract unique frames from video with PiP masking."""

    def __init__(
        self,
        fps: float = 0.5,
        hash_size: int = 16,
        dhash_threshold_pct: float = 0.05,
        gap_fill_max_sec: float = 15.0,
        scene_boundary_dist_pct: float = 0.20,
        scene_boundary_gap_sec: float = 10.0,
        cooldown_sec: float = 4.0,
        cooldown_consecutive: int = 3,
    ) -> None: ...

    async def extract(
        self, video_path: Path, output_dir: Path
    ) -> FrameSamplingResult:
        """Full pipeline: extract → dHash dedup → PiP mask → gap fill → scenes."""
        ...
```

**Кроки:**
1. FFmpeg fps extraction → temp JPEG files
2. PiP detection via temporal diff → mask rect
3. dHash computation (з PiP mask) → dedup
4. Gap fill (no gap > 15s) → додаткові frames
5. Scene boundary detection → Scene grouping
6. Return `FrameSamplingResult`

**Залежності:** `opencv-python-headless`, `Pillow`, `imagehash`, FFmpeg (system)

**Acceptance criteria:**
- На тестовому 10-хв відео дає ~17 frames (як spike)
- На 27-хв відео дає ~91 frames
- PiP detection confidence >0.5
- Async FFmpeg (через `asyncio.create_subprocess_exec`)

> **ПЕРЕД НАСТУПНОЮ ЗАДАЧЕЮ** — виконати **CP-1: Перевірка Frame Sampler** (див. нижче)

---

### CP-1: Перевірка Frame Sampler (людина)

#### Контекст для перевіряючого

Frame Sampler — це перший етап обробки відео. Він витягує з відео **унікальні кадри** (скріншоти), відкидаючи дублікати. Це як зробити фотознімки кожного нового слайду або нового фрагменту коду, що з'являється на екрані. Якість цього етапу визначає все далі: якщо кадр пропущений — весь контент на ньому буде втрачений для аналізу.

У лекційних відео курсів програмування лектор показує: слайди, код у IDE, термінал, діаграми. Часто є PiP (Picture-in-Picture) — кругле або прямокутне відео лектора в куті екрану. PiP постійно рухається (лектор жестикулює) і може створювати хибні "зміни кадру". Тому PiP зону потрібно маскувати (ігнорувати).

#### Що підготувати

1. Тестове відео (є в проекті, ~10 хв або ~27 хв)
2. Результат роботи `FrameSampler.extract()` — список кадрів (JPEG) + `FrameSamplingResult`
3. Згенерувати HTML gallery кадрів (thumbnail + timestamp + scene_id + hamming_from_prev)

#### Покрокова перевірка

**Крок 1: Покриття (10 хвилин)**

Відкрити відео і gallery поруч. Прогнати відео на 2x швидкості. Кожен раз коли на відео з'являється НОВИЙ контент (інший слайд, новий фрагмент коду, інше вікно) — перевірити що відповідний кадр є в gallery.

- Що добре: кожна зміна контенту є в кадрах
- Червоний прапорець: **пропущена зміна** — новий слайд або код з'явився, а кадру немає. Записати timestamp.

**Крок 2: Дублікати (5 хвилин)**

Переглянути gallery послідовно. Порівняти сусідні кадри очима.

- Що добре: кожен наступний кадр суттєво відрізняється від попереднього
- Червоний прапорець: **два однакових кадри** (або кадри з мінімальною різницею — наприклад тільки курсор зсунувся). Це означає що dHash threshold надто низький.

**Крок 3: PiP маска (3 хвилини)**

У `FrameSamplingResult` є `pip_mask: (x1, y1, x2, y2)` — координати зони лектора. Відкрити 3-5 кадрів, перевірити:

- Що добре: PiP mask повністю покриває камеру лектора, не зачіпає основний контент
- Червоний прапорець: маска **зсунута** (частина коду/слайду потрапила в mask і ігнорується), або маска **замала** (частина камери лектора не покрита → створює хибні зміни)
- Червоний прапорець: `pip_mask = None` але на відео є камера лектора

**Крок 4: Scene boundaries (5 хвилин)**

У gallery кадри згруповані по scene_id. Перевірити 5-7 boundaries:

- Що добре: нова сцена = нова тема/слайд/файл на екрані
- Червоний прапорець: один слайд **розбитий на 2 сцени** (середина слайду — це не boundary)
- Червоний прапорець: два різних слайди **в одній сцені** (boundary пропущена)

**Крок 5: Gap fill (2 хвилини)**

Знайти gap-fill кадри (`is_gap_fill: true` в результатах). Перевірити:

- Що добре: gap-fill кадри заповнюють проміжки >15 секунд між golden frames
- Червоний прапорець: gap-fill кадр **ідентичний сусідньому** golden frame (марна витрата)

#### Які ризики перевіряємо

| Ризик | Наслідок якщо пропустити |
|-------|--------------------------|
| Пропущений кадр зі слайдом | Весь текст слайду не буде витягнутий — пробіл у навчальному матеріалі |
| Пропущений кадр з кодом | Фрагмент коду втрачений — студент не побачить приклад |
| PiP mask на контенті | Частина коду/тексту буде ігноруватись як "рух камери" |
| Забагато кадрів (>200 для 10 хв) | Зайві витрати на Vision LLM, повільна обробка |

#### Рішення

- **Pass:** Всі зміни контенту покриті, немає дублікатів, PiP mask коректна, scenes логічні
- **Fail:** Пропущено ≥1 значущу зміну контенту, або PiP mask на контенті → повернутись до VD-002

---

#### VD-003: visual_analyzer.py — Eyes step

Port з `scripts/spike_vd_multimodel.py` (Eyes step). Інтеграція з `ModelRouter` або прямий Gemini SDK.

```python
class VisualAnalyzer:
    """Per-frame Vision LLM analysis with context."""

    def __init__(
        self,
        model: str = "gemini-3.1-flash-lite-preview",
        rpm_limit: int = 15,
        context_max_gap_sec: float = 7.0,
        max_context_images: int = 2,
    ) -> None: ...

    async def analyze_scene(
        self,
        scene: Scene,
        frames: list[SampledFrame],
        frame_dir: Path,
        course_context: str = "",
    ) -> list[EyesResult]:
        """Analyze all frames in a scene sequentially."""
        ...
```

**Кроки per frame:**
1. Build context: previous frames (within 7s, same scene, max 2)
2. Build prompt: course context + scene context (prev frame descriptions) + prompt v3
3. Call Vision LLM with 1-3 images
4. Parse Markdown response → EyesResult
5. Rate limiting via key pool / semaphore

**Prompt:** Prompt v3 з spike (language-agnostic Markdown). Зберегти в `vd/prompts/eyes_v3.txt`.

**Відкрите питання:** Використовувати `ModelRouter` чи прямий `google.genai`?
- `ModelRouter` — єдиний інтерфейс, fallback, logging в DB
- Прямий SDK — простіше, менше abstractions, key pool вже є в `key_pool.py`
- **Рекомендація:** `ModelRouter` з єдиним provider (Gemini lite), щоб мати logging і можливість fallback у майбутньому

**Acceptance criteria:**
- 10 GT frames → accuracy ≥95% (виправлений GT)
- Rate limiting працює (не більше 15 RPM)
- Resumable: якщо перервано — продовжує з місця зупинки

> **ПЕРЕД НАСТУПНОЮ ЗАДАЧЕЮ** — виконати **CP-2: Перевірка Visual Analyzer** (див. нижче)

---

### CP-2: Перевірка Visual Analyzer / Eyes (людина)

#### Контекст для перевіряючого

Visual Analyzer ("Eyes") — це крок де штучний інтелект (Vision LLM) **дивиться на кожен кадр** і описує що бачить. Для кадру з кодом — він повинен витягти точний текст коду зі збереженням форматування. Для слайду — витягти заголовок і тіло тексту. Для терміналу — команди і вивід.

Це найважливіший крок з точки зору якості. Якщо модель:
- **Пропустить** фрагмент коду — він буде втрачений
- **Змінить** символ (`_` → `-`, `==` → `=`) — код стане невірним
- **Додасть** рядок якого немає на екрані (hallucination) — з'явиться хибна інформація
- Не зможе **прочитати** дрібний текст — частина контенту втрачена

Під час spike ми досягли 99.3% accuracy на тестових кадрах. Але production кадри можуть відрізнятись.

#### Що підготувати

1. Результат CP-1: набір витягнутих кадрів (JPEG файли)
2. Результат `VisualAnalyzer.analyze_scene()` — `EyesResult` для кожного кадру (Markdown response)
3. Інструмент для side-by-side перегляду: зображення кадру ліворуч, response LLM праворуч (наприклад `scripts/trace_frame.py` або простий HTML viewer)

#### Покрокова перевірка

Обрати **10 кадрів** з різних типів сцен:
- 3 кадри з кодом в IDE (Python editor — основний use case)
- 3 кадри зі слайдами (заголовок + текст + code examples)
- 2 кадри з терміналом (команди + вивід)
- 1 кадр з діаграмою
- 1 кадр з talking head (лектор без контенту)

**Для кожного з 10 кадрів виконати:**

**Крок 1: Повнота витягування (1 хв на кадр)**

Відкрити кадр (зображення). Подивитись що на ньому видно. Потім прочитати response LLM.

- Що добре: кожен видимий code block, текст, заголовок, команда терміналу — описані і витягнуті
- Червоний прапорець: **пропущений блок** — на кадрі видно 3 code blocks, а в response тільки 2
- Червоний прапорець: **пропущений slide text** — заголовок слайду або bullet points не витягнуті

**Крок 2: Точність коду (2 хв на кадр з кодом)**

Для кадрів з кодом — порівняти код з кадру з кодом в response **посимвольно**. Особливу увагу на:

- `_` (underscores) — чи правильно (`__init__` не стало `_init_` або `init`)
- `->` (return type annotations) — чи не стало `>` або `→`
- Відступи (indentation) — чи збережені 4 пробіли
- Числа — чи правильні (`11111` не стало `1111`)
- Лапки — чи правильний тип (`'` vs `"`)
- Оператори — чи не змінились (`==` не стало `=`, `+` не стало `*`)

- Що добре: код в response ідентичний коду на кадрі
- Червоний прапорець: **будь-яка зміна символів** у коді

**Крок 3: Hallucinations (1 хв на кадр)**

Перевірити чи є в response інформація якої **НЕМАЄ на кадрі**:

- Що добре: response описує тільки те що видно
- Червоний прапорець: **код якого немає на екрані** — модель "домислила" наступний рядок функції
- Червоний прапорець: **текст з-за popup** — на кадрі autocomplete popup перекриває код, а модель "побачила" код під popup

**Крок 4: Talking head (1 хв)**

Для кадру де лектор просто говорить без контенту:

- Що добре: response коротко описує що видно ("person speaking, no content change"), code blocks відсутні
- Червоний прапорець: **вигаданий код або текст** у response для talking head кадру

#### Які ризики перевіряємо

| Ризик | Наслідок | Як виявити |
|-------|----------|------------|
| Hallucination коду | Студент побачить код якого не було в лекції | Порівняти response з кадром — зайві рядки |
| Пропущений code block | Приклад з лекції втрачений | На кадрі є код, в response немає |
| Змінені символи в коді | Код не буде працювати (`__init__` → `_init_`) | Посимвольне порівняння |
| UI noise в code blocks | Sidebar, status bar потрапили в "код" | Перевірити що code blocks містять тільки код |

#### Рішення

- **Pass:** 9/10 кадрів мають повний і точний витяг; допускається 1 мінорна помилка (порядок елементів)
- **Fail:** ≥2 кадри з пропущеним кодом, або ≥1 кадр з hallucination → повернутись до VD-003

---

#### VD-004: memory_pipeline.py — Hierarchical memory

Port з spike. Три рівні aggregation.

```python
class MemoryPipeline:
    """Hierarchical text-only memory: instant → scene → course."""

    async def process_scene(
        self,
        eyes_results: list[EyesResult],
        scene: Scene,
        course_memory: CourseMemory,
    ) -> tuple[InstantMemory, SceneMemory, CourseMemory]:
        """Process one scene through all memory levels."""
        instant = self._instant_merge(eyes_results)
        scene_mem = await self._scene_synthesize(eyes_results, instant, scene)
        updated_course = await self._update_course_memory(course_memory, scene_mem)
        return instant, scene_mem, updated_course
```

**Instant merge:** Code-based overlap detection (no LLM). Fallback на LLM якщо separators зашкалюють.

**Scene synthesis:** LLM call (той самий lite model) → scene_type, summary, topics, importance.

**Course memory:** LLM call → оновлений running context ≤200 слів.

**Acceptance criteria:**
- golden_075 merge recovers occluded code (100% merged accuracy)
- Course memory не росте безмежно (≤200 слів)
- Scene synthesis дає правильний scene_type для slide/code/terminal

> **ПЕРЕД НАСТУПНОЮ ЗАДАЧЕЮ** — виконати **CP-3: Перевірка Memory Pipeline** (див. нижче)

---

### CP-3: Перевірка Memory Pipeline (людина)

#### Контекст для перевіряючого

Memory Pipeline об'єднує результати кількох кадрів в осмислену інформацію. Це три рівні:

1. **Instant Memory** — об'єднує описи кількох кадрів ОДНІЄЇ сцени в один текст. Наприклад, якщо лектор показує довгий код і ми зробили 4 знімки (кожен з частиною коду) — instant merge збирає їх в один повний фрагмент.

2. **Scene Memory** — робить резюме сцени: тип (слайд/код/термінал), тематика, важливість 1-5. Як "конспект" однієї сцени.

3. **Course Memory** — "бігучий контекст" на рівні всього відео (≤200 слів). Передається наступній сцені щоб модель знала про що йде курс. Як "чернетка конспекту всієї лекції".

**Головний ризик цього кроку: СПОТВОРЕННЯ при merge.** Під час spike ми виявили що merge може ЗМІНИТИ код — наприклад оператор `+` став `*`, число `0.937` стало `0.837`. Це катастрофічна помилка — код виглядає правильним але працює неправильно.

#### Що підготувати

1. Обрати **5 scenes з 3+ кадрами** (де instant merge реально працює — для сцен з 1 кадром merge тривіальний)
2. Для кожної сцени зібрати:
   - Оригінальні Eyes responses (per-frame) — що модель побачила на кожному кадрі
   - Instant Memory merged_text — що вийшло після merge
   - Scene Memory — резюме сцени
3. Також підготувати Course Memory на різних етапах відео (початок, середина, кінець)

#### Покрокова перевірка

**Крок 1: Instant merge — збереження коду (15 хвилин, найважливіший)**

Для кожної з 5 scenes:

Відкрити окремі Eyes responses (кадр 1, кадр 2, кадр 3...) і merged_text поруч.

a) **Повнота:** Весь код з окремих кадрів є в merged_text?
   - Що добре: якщо кадр 1 має `def foo():`, кадр 2 має `def foo():` + `return x` — merged має і те і те
   - Червоний прапорець: **код з одного кадру зник** після merge

b) **Дублікати:** Чи немає повторів?
   - Що добре: код який є на 2-3 кадрах (overlap) — з'являється в merged тільки один раз
   - Червоний прапорець: **один і той самий фрагмент повторюється** 2-3 рази

c) **Цілісність (КРИТИЧНО):** Чи не ЗМІНИВСЯ код при merge?
   - Відкрити оригінальний Eyes response і merged_text. Порівняти code blocks **посимвольно**.
   - Червоний прапорець: **будь-яка зміна символів** — особливо оператори (`+`→`*`), числа (`0.937`→`0.837`), імена змінних
   - Це саме та помилка яку ми знайшли під час spike (2.5-flash merge corruption). У lite вона не виявлена, але треба перевірити на production даних.

**Крок 2: Scene Memory — адекватність (5 хвилин)**

Для кожної з 5 scenes прочитати scene memory:

- `scene_type` — відповідає реальності? Слайд = "slide", код в IDE = "code_editing", вивід терміналу = "terminal"
- `summary` — осмислений, 2-3 речення українською, описує суть сцени?
- `topics` — релевантні? (наприклад для сцени з `int()` і `float()` — topics мають включати ці терміни)
- `importance` — логічна? (talking head = 1-2, ключовий code example = 4-5)

- Червоний прапорець: **generic summary** типу "На екрані показано код" без деталей
- Червоний прапорець: **wrong scene_type** — термінал описаний як слайд

**Крок 3: Course Memory — еволюція (5 хвилин)**

Прочитати Course Memory на 3 точках відео:
- Після scene 3 (початок)
- Після scene ~50% (середина)
- Фінальний (кінець)

- Що добре: контекст еволюціонує, відображає пройдені теми, ≤200 слів
- Червоний прапорець: **drift** — після 50+ сцен контекст став generic ("курс про Python"), втративши конкретику
- Червоний прапорець: **overflow** — текст значно більше 200 слів (модель ігнорує ліміт)
- Червоний прапорець: **стагнація** — контекст не змінюється між сценами (модель копіює попередній)

#### Які ризики перевіряємо

| Ризик | Наслідок | Як виявити |
|-------|----------|------------|
| Merge corruption (зміна коду) | Невірний код в навчальних матеріалах | Посимвольне порівняння merged vs original |
| Втрата коду при merge | Приклад з лекції зникає | Код є в Eyes response але немає в merged |
| Дублікати після merge | Один приклад повторюється 3 рази | Прочитати merged_text — шукати повтори |
| Course memory drift | Контекст для наступних сцен стає безглуздим | Прочитати фінальний course memory |

#### Рішення

- **Pass:** Merge не змінює код (0 corruption), scene types вірні, course memory осмислений
- **Fail:** Хоча б 1 випадок merge corruption → повернутись до VD-004 (виправити merge algorithm)

---

#### VD-005: pipeline.py — Orchestrator

```python
class VDPipeline:
    """Orchestrate Frame Sampling → Visual Analysis → Memory.

    Self-contained: нічого не знає про STT. Повертає VDResult.
    """

    def __init__(
        self,
        sampler: FrameSampler,
        analyzer: VisualAnalyzer,
        memory: MemoryPipeline,
    ) -> None: ...

    async def process(self, video_path: Path) -> VDResult:
        """Full VD pipeline: frames → eyes → memory."""
        temp_dir = Path(tempfile.mkdtemp(prefix="vd_frames_"))
        try:
            # Stage A
            sampling = await self.sampler.extract(video_path, temp_dir)

            # Stage B: scene by scene
            course_memory = CourseMemory(text="", scenes_covered=0)
            scene_analyses: list[SceneAnalysis] = []

            for scene in sampling.scenes:
                scene_frames = [f for f in sampling.frames if f.scene_id == scene.scene_id]
                eyes = await self.analyzer.analyze_scene(
                    scene, scene_frames, temp_dir, course_memory.text
                )
                instant, scene_mem, course_memory = await self.memory.process_scene(
                    eyes, scene, course_memory
                )
                scene_analyses.append(
                    SceneAnalysis(scene=scene, eyes_results=eyes,
                                 instant_memory=instant, scene_memory=scene_mem)
                )

            return VDResult(
                scenes=scene_analyses,
                course_memory=course_memory,
                frames_total=len(sampling.frames),
                frames_analyzed=len(sampling.frames),
                model=self.analyzer.model,
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
```

**Acceptance criteria:**
- E2E на 10-хв відео: frames → eyes → memory → result
- Temp cleanup працює (навіть при exceptions)
- Resumable state (optional: save intermediate state для long videos)

> **ОБОВ'ЯЗКОВО ПЕРЕД INTEGRATION** — виконати **CP-4: Gate Review VD Pipeline** (див. нижче).
> Це головний checkpoint. Не переходити до Фази 5 поки CP-4 не пройдено.

---

### CP-4: Gate Review — VD Pipeline E2E (людина) ⛔ GATE

#### Контекст для перевіряючого

Це **головний checkpoint** всього VD module. На цьому етапі повний VD pipeline працює ізольовано: відео подається на вхід, на виході — `VDResult` з описами всіх сцен, витягнутим кодом і текстом, ієрархічною пам'яттю.

Після цього checkpoint'у ми починаємо **інтеграцію** з рештою системи (STT, database, API). Виправляти проблеми VD module після інтеграції буде значно складніше і дорожче. Тому тут ми витрачаємо більше часу на перевірку.

**Перевірка виконується на 2 повних відео:**
- **Відео 1:** Лекція зі слайдами (~10 хв) — основний тип контенту
- **Відео 2:** Відео з кодом і терміналом (~27 хв) — найскладніший тип

Для кожного відео потрібно 1 годину на перевірку.

#### Що підготувати

1. Запустити VD pipeline на обох відео, отримати `VDResult`
2. Згенерувати **повний звіт** (скрипт або notebook):
   - Загальна статистика: кількість frames, scenes, час обробки
   - Timeline: хронологічний список scenes з timestamps, types, summaries
   - Для кожної сцени: thumbnails кадрів + scene memory summary
   - Фінальний course memory

#### Покрокова перевірка (для кожного відео)

**Крок 1: Coverage — повнота покриття (15 хвилин)**

Відкрити відео і timeline звіту поруч. Прогнати відео на 2x.

Для кожного "розділу" лекції перевірити: чи є відповідні scenes у звіті?

- Що добре: кожна змістовна частина лекції (новий слайд, новий код, нова тема) представлена як scene
- Червоний прапорець: **missing segment** — 3+ хвилини відео без жодної scene. Це означає що Frame Sampler або Eyes пропустили цілий фрагмент лекції.
- Червоний прапорець: **зайві scenes** — 10+ scenes за 1 хвилину (надто гранулярно, марна витрата)

**Крок 2: Code accuracy — точність коду (20 хвилин)**

Обрати **5 scenes з кодом** (де scene_type = "code_editing" або де scene memory містить code blocks). Для кожної:

a) Відкрити відео на відповідному timestamp, поставити на паузу
b) Прочитати `instant_memory.merged_text` — порівняти з екраном
c) Перевірити: весь код з екрану є в merged_text? Код точний?

- Що добре: merged_text містить весь видимий код, точно
- Червоний прапорець: пропущений код, змінені символи, зайвий код (hallucination)

**Крок 3: Slide accuracy (10 хвилин)**

Обрати **3 scenes зі слайдами**. Перевірити:

- Заголовок слайду є в scene memory?
- Ключові пункти слайду (bullet points) відображені?
- Code examples на слайді витягнуті?

**Крок 4: Timeline coherence (5 хвилин)**

Прочитати scenes **послідовно** як "конспект лекції":

- Що добре: scenes йдуть хронологічно, кожна наступна логічно продовжує попередню
- Червоний прапорець: **порушена хронологія** (scene 15 timestamp < scene 14)
- Червоний прапорець: **зміст не складається** в логічну послідовність

**Крок 5: Course memory (5 хвилин)**

Прочитати фінальний `course_memory.text`:

- Що добре: осмислене резюме лекції, згадує ключові теми і приклади
- Червоний прапорець: generic текст без конкретики, або безглуздий набір слів

**Крок 6: Performance (2 хвилини)**

Перевірити час обробки:

- 10-хв відео: очікувано 5-15 хвилин
- 27-хв відео: очікувано 15-40 хвилин
- Червоний прапорець: >1 годину на 10-хв відео → проблема з rate limiting або retry loops

#### Які ризики перевіряємо (зведення)

| Ризик | Критичність | Як виявити |
|-------|-------------|------------|
| Пропущений контент | Високий | Крок 1: відео vs timeline |
| Невірний код | Критичний | Крок 2: merged_text vs екран |
| Hallucination | Критичний | Крок 2: зайвий код якого немає |
| Pipeline crash / timeout | Високий | Крок 6: час обробки |
| Безглуздий course memory | Середній | Крок 5 |

#### Рішення

- **Pass:** Coverage >90% змісту, code accuracy >95%, 0 hallucinations, адекватний course memory, час в межах очікувань. **Можна переходити до інтеграції.**
- **Conditional pass:** Мінорні проблеми (1-2 пропущені scenes, 1 неточність в коді) — задокументувати як known issues, продовжити
- **Fail:** Coverage <80%, або ≥1 hallucination, або merge corruption → **СТОП. Повернутись до відповідної задачі VD-002/003/004.**

---

### Фаза 5: Integration

#### VD-006: Redesign VideoProcessor

Оновити `src/course_supporter/ingestion/video.py`:

```python
class VideoProcessor(SourceProcessor):
    async def process(self, source: MaterialEntry, *, router=None) -> SourceDocument:
        video_path = await self._download(source.source_url)
        audio_path = await self._extract_audio(video_path)

        # STT і VD — повністю паралельно, незалежні потоки
        stt_task = asyncio.create_task(self._stt.transcribe(audio_path))
        vd_task = asyncio.create_task(self._vd.process(video_path))

        stt_result, vd_result = await asyncio.gather(stt_task, vd_task)

        # Cross-modal alignment ПІСЛЯ обох потоків
        aligned, report = self._aligner.align(stt_result, vd_result)

        # Convert to SourceDocument
        chunks = self._build_chunks(aligned, report)
        return SourceDocument(source_type=SourceType.VIDEO, chunks=chunks, ...)
```

**Ключова зміна:** STT і VD запускаються **повністю паралельно** через `asyncio.gather`. VD нічого не знає про STT. Alignment — окремий крок після обох.

#### VD-007: Cross-modal Alignment (ingestion level)

**Належить до `src/course_supporter/ingestion/alignment.py`** (НЕ до `vd/`).

```python
class CrossModalAligner:
    """Align STT transcript with VD scene analysis.

    Працює ПІСЛЯ того як обидва потоки повністю оброблені.
    """

    WINDOW_SEC: float = 10.0

    def align(
        self,
        stt: TranscriptResult,
        vd: VDResult,
    ) -> tuple[list[AlignedSegment], AlignmentReport]:
        # Phase 1: Temporal alignment
        pairs = self._temporal_match(stt.segments, vd.scenes)
        # Phase 2: Semantic cross-reference
        self._semantic_link(pairs)
        # Phase 3: Conflict detection (VD wins for code)
        self._detect_conflicts(pairs)
        # Phase 4: Verification report
        report = self._verify(pairs)
        return pairs, report
```

**Acceptance criteria:**
- Temporal alignment коректний (manual verification)
- Conflicts detected: тест з відомою розбіжністю (fa12/faa12)
- Coverage gaps >30s flagged
- AlignmentReport дає actionable інформацію

**Складність:** Середня. Алгоритмічна робота з timestamps і text matching (не LLM).

> **ПЕРЕД НАСТУПНОЮ ЗАДАЧЕЮ** — виконати **CP-5: Перевірка Cross-modal Alignment** (див. нижче)

---

### CP-5: Перевірка Cross-modal Alignment (людина)

#### Контекст для перевіряючого

Cross-modal Alignment — це крок де результати двох незалежних аналізів з'єднуються:

- **STT (Speech-to-Text):** Що лектор **каже** (текст з аудіо, з timestamps)
- **VD (Visual Description):** Що **показує екран** (описи кадрів, код, слайди, з timestamps)

Ці два потоки описують ту саму лекцію, але з різних "каналів". Alignment їх з'єднує: для кожного моменту відео ми маємо знати ЩО лектор каже І ЩО видно на екрані.

**Чому це складно:**
- Лектор може сказати "давайте подивимось на код" за 3 секунди ДО того як код з'явиться на екрані
- Лектор може оговоритись: сказати "fa12" а на екрані буде "faa12" — екран правильний
- Лектор може пояснювати одну тему, а на екрані ще попередній слайд (переходить)
- Деякі моменти — тільки звук (лектор пояснює без змін на екрані)
- Деякі моменти — тільки візуальне (лектор мовчить а на екрані з'являється результат)

#### Що підготувати

1. Запустити повний pipeline (STT + VD + alignment) на тестовому відео
2. Отримати `AlignmentReport` і список `AlignedSegment[]`
3. Відкрити відео в плеєрі з можливістю стрибати по timestamps

#### Покрокова перевірка

**Крок 1: Огляд AlignmentReport (5 хвилин)**

Прочитати автоматичний звіт:

- `coverage_gaps` — чи є проміжки >30 секунд без контенту? Якщо є — перевірити у відео: це пауза лектора або пропущений контент?
- `vd_orphans` — VD scenes без STT match. Якщо це talking_head або коротка (<5с) сцена — OK. Якщо це сцена з кодом — проблема.
- `stt_orphans` — STT segments без VD. Якщо лектор просто говорить без змін на екрані — OK.
- `semantic_coverage` — % пар де STT і VD семантично пов'язані. Очікувано >60%.
- `conflicts` — список конфліктів. Кожен перевірити вручну (Крок 3).

**Крок 2: Temporal alignment — 10 випадкових пар (15 хвилин)**

Обрати 10 `AlignedSegment` з різних частин відео. Для кожного:

a) Відкрити відео на `start_sec`
b) Послухати що лектор каже (STT text)
c) Подивитись що на екрані (VD scene)
d) Запитати себе: ці дві речі **про те саме**?

- Що добре: лектор каже "давайте подивимось на функцію int()" і на екрані код з `int()`
- Прийнятно: лектор ще закінчує пояснення попередньої теми, а слайд вже новий (temporal offset ≤5с)
- Червоний прапорець: лектор говорить про **зовсім іншу тему** ніж те що на екрані, і offset >10с — alignment помилився

**Крок 3: Перевірка конфліктів (10 хвилин)**

Для кожного конфлікту з `AlignmentReport.conflicts`:

a) Відкрити відео на timestamp конфлікту
b) Визначити: конфлікт реальний чи хибний?

Реальний конфлікт (правильно виявлений):
- Лектор каже "fa12", на екрані "faa12" → conflict правильний, VD wins

Хибний конфлікт (false positive):
- Лектор каже "клас int" (українською), VD показує `class int` (англійською) → це НЕ конфлікт, це одне й те саме різними мовами
- Лектор каже "підкреслення", VD показує `_` → не конфлікт

- Що добре: >80% конфліктів реальні
- Червоний прапорець: >50% хибних конфліктів → алгоритм conflict detection надто агресивний

**Крок 4: Спеціальний тест — known conflict (5 хвилин)**

Якщо у відео є місце де лектор **доказано помиляється** (каже не те що на екрані) — перевірити чи alignment це виявив. Наприклад: лектор каже неправильне число, а на екрані правильне.

- Що добре: конфлікт виявлений, VD value marked as winner
- Червоний прапорець: конфлікт **не виявлений** — alignment пропустив реальну розбіжність

#### Які ризики перевіряємо

| Ризик | Наслідок | Як виявити |
|-------|----------|------------|
| Wrong temporal match | STT і VD з різних частин лекції з'єднані | Крок 2: вибіркова перевірка 10 пар |
| Missed conflict | Невірна інформація від лектора потрапить в матеріали без позначки | Крок 4: known conflict test |
| Too many false positives | Зайвий шум — кожен "конфлікт" треба перевіряти вручну | Крок 3: >50% хибних |
| Coverage gaps | Частина лекції не покрита | Крок 1: AlignmentReport.coverage_gaps |

#### Рішення

- **Pass:** ≥8/10 пар correctly aligned, >80% конфліктів реальні, known conflicts виявлені
- **Fail:** ≤5/10 пар correct, або known conflict пропущений → повернутись до VD-007

---

#### VD-008: Update factory.py + config

- Wire `VDPipeline` в `create_video_processor()`
- Config: Gemini key pool, RPM limits, model name
- `external_services.yaml`: один новий action `visual_eyes`

#### VD-009: ChunkType enum + Alembic migration

- Додати: `VISUAL_SCENE`, `VISUAL_CODE`, `VISUAL_SLIDE`, `VISUAL_TERMINAL`, `ALIGNED_SEGMENT`
- Alembic migration для enum extension

#### VD-010: Update MergeStep

- `MergeStep` має обробляти нові `ChunkType` значення
- Aligned segments мають priority metadata для downstream agents

#### VD-011: E2E test

- Один повний прохід: відео → STT + VD → SourceDocument
- Verify: chunks покривають все відео, alignment report без critical gaps

> **ПІСЛЯ E2E ТЕСТУ** — виконати **CP-6: Фінальна перевірка SourceDocument** (див. нижче)

---

### CP-6: Фінальна перевірка SourceDocument (людина)

#### Контекст для перевіряючого

Це фінальний checkpoint. На вході було відео лекції. На виході — `SourceDocument`: структурований документ з chunks (фрагментами), де кожен chunk має тип, timestamp, і текст. Цей документ далі використовується AI-агентом (`ArchitectAgent`) для побудови структури курсу.

Якість `SourceDocument` визначає якість всього downstream: якщо документ неповний або містить помилки — AI-агент побудує неповну або невірну структуру курсу.

#### Що підготувати

1. Запустити повний E2E pipeline на тестовому відео
2. Отримати `SourceDocument` з chunks
3. Експортувати chunks в читабельний формат (Markdown або HTML):
   - Для кожного chunk: `[timestamp] [ChunkType] content`
   - Впорядковані хронологічно

#### Покрокова перевірка

**Крок 1: Прочитати як документ (15 хвилин)**

Прочитати всі chunks **послідовно**, як якби це був конспект лекції. Запитати себе:

- Чи можна зрозуміти про що ця лекція, читаючи тільки chunks?
- Чи є логічна послідовність (вступ → основна частина → приклади → висновки)?
- Чи є весь ключовий код з лекції?

- Що добре: документ читається як осмислений конспект з кодом і поясненнями
- Червоний прапорець: **безлад** — chunks в хаотичному порядку, код без контексту, пояснення без коду

**Крок 2: ChunkType balance (5 хвилин)**

Порахувати кількість chunks по типах:

| ChunkType | Очікувана частка | Якщо значно менше |
|-----------|-----------------|-------------------|
| TRANSCRIPT | 30-50% | STT не працює |
| VISUAL_SCENE | 10-20% | VD не працює |
| VISUAL_CODE | 10-30% | Код не витягується |
| VISUAL_SLIDE | 5-15% | Слайди не витягуються |
| ALIGNED_SEGMENT | 10-30% | Alignment не працює |

- Червоний прапорець: 90% TRANSCRIPT, 2% VISUAL — VD pipeline не дав результатів
- Червоний прапорець: 0% ALIGNED_SEGMENT — alignment повністю не працює

**Крок 3: Порівняння з baseline (10 хвилин)**

Якщо є попередня версія SourceDocument (без VD, тільки STT) — порівняти:

- Скільки нової інформації додав VD? (код з екрану, текст слайдів)
- Чи не зіпсував VD те що вже було? (STT chunks не пропали?)
- Яка додаткова цінність? (чи вартувало всіх цих зусиль?)

- Що добре: VD додав 30-50% нового контенту (код, слайди) яких не було в STT
- Червоний прапорець: VD додав <5% нового — pipeline не виправдовує свою складність

**Крок 4: Downstream test (15 хвилин, опціонально)**

Передати SourceDocument в `ArchitectAgent` (або mock) і подивитись чи покращилась структура курсу:

- Чи AI-агент використовує code blocks з VD?
- Чи структура курсу стала точнішою/повнішою?

#### Які ризики перевіряємо

| Ризик | Наслідок | Як виявити |
|-------|----------|------------|
| VD chunks не потрапили в документ | Весь VD pipeline марний | Крок 2: ChunkType balance |
| Chunks в неправильному порядку | Конспект лекції безглуздий | Крок 1: прочитати послідовно |
| STT chunks зникли | Регресія — втратили те що працювало | Крок 3: порівняння з baseline |
| ArchitectAgent ігнорує VD | Downstream не бачить покращення | Крок 4: downstream test |

#### Рішення

- **Pass:** Документ читається як конспект, VD додав ≥20% нового контенту, ChunkTypes збалансовані. **Готово до production.**
- **Fail:** VD не додає цінності, або зламав STT → переглянути integration (VD-006, VD-010)

---

### Фаза 6: Polish

#### VD-012: Unit tests
- `test_frame_sampler.py` — mock FFmpeg, test dHash/PiP/gap_fill/scenes
- `test_visual_analyzer.py` — mock Gemini, test prompt building, rate limiting
- `test_memory_pipeline.py` — test merge logic, scene synthesis
- `test_alignment.py` — test temporal matching, conflict detection

#### VD-013: Config env vars
- `VD_GEMINI_KEYS` — key pool (comma-separated)
- `VD_MODEL` — default model name
- `VD_RPM_LIMIT` — rate limit
- `VD_FPS`, `VD_HASH_SIZE`, `VD_DHASH_THRESHOLD` — sampling params

#### VD-014: Temp files cleanup
- Verify `shutil.rmtree` works in all paths (success, error, cancellation)
- ARQ task cleanup on worker restart

---

## 6. Залежності (оновлені)

```toml
[project.optional-dependencies]
vd = [
    "opencv-python-headless>=4.9",
    "Pillow>=10.0",
    "ImageHash>=4.3",
]
# Stage C (OCR) — ВИДАЛЕНО, не потрібен
```

---

## 7. Порядок виконання та оцінка

| # | Задача | Що | Хто | Орієнтовно |
|---|--------|----|----|------------|
| | **Фаза 4: VD Module** | | | |
| 1 | VD-001 schemas | Pydantic models | dev | 1 сесія |
| 2 | VD-002 frame_sampler | Stage A (port spike + async) | dev | 1-2 сесії |
| 3 | **CP-1** | **Перевірка Frame Sampler** | **людина** | **30 хв** |
| 4 | VD-003 visual_analyzer | Eyes (port + ModelRouter) | dev | 1-2 сесії |
| 5 | **CP-2** | **Перевірка Visual Analyzer** | **людина** | **1 год** |
| 6 | VD-004 memory_pipeline | Hierarchical memory (port) | dev | 1 сесія |
| 7 | **CP-3** | **Перевірка Memory Pipeline** | **людина** | **30 хв** |
| 8 | VD-005 pipeline | VD orchestrator | dev | 0.5 сесії |
| 9 | **CP-4** | **⛔ Gate Review — VD Pipeline E2E (2 відео)** | **людина** | **2 год** |
| | **Фаза 5: Integration** | | | |
| 10 | VD-006 VideoProcessor | Redesign (STT ‖ VD parallel) | dev | 1-2 сесії |
| 11 | VD-007 alignment | Cross-modal STT↔VD | dev | 2-3 сесії |
| 12 | **CP-5** | **Перевірка Alignment** | **людина** | **1 год** |
| 13 | VD-008 factory + config | Wire VDPipeline, env vars | dev | 0.5 сесії |
| 14 | VD-009 ChunkType + migration | Enum extension + Alembic | dev | 0.5 сесії |
| 15 | VD-010 MergeStep update | Handle new ChunkTypes | dev | 1 сесія |
| 16 | VD-011 E2E test | Full pipeline test | dev | 1 сесія |
| 17 | **CP-6** | **Фінальна перевірка SourceDocument** | **людина** | **1-2 год** |
| | **Фаза 6: Polish** | | | |
| 18 | VD-012 unit tests | Per-module tests | dev | 1-2 сесії |
| 19 | VD-013 config env vars | Env-based configuration | dev | 0.5 сесії |
| 20 | VD-014 temp cleanup | Verify cleanup in all paths | dev | 0.5 сесії |
| | **Всього** | **dev: ~12-16 сесій + людина: ~5-6 годин** | | |

**Критичний шлях:** VD-001 → VD-002 → **CP-1** → VD-003 → **CP-2** → VD-004 → **CP-3** → VD-005 → **⛔ CP-4** → VD-006 → VD-007 → **CP-5** → VD-008..011 → **CP-6**

**CP-4 — gate checkpoint.** Не починати Фазу 5 поки CP-4 не passed.

---

## 8. Відкриті питання для обговорення

### Q1: ModelRouter vs direct SDK для Eyes?

**Pros ModelRouter:** logging, fallback, unified interface, LLM cost tracking
**Pros direct SDK:** простіше, менше abstraction layers, key_pool.py вже є
**Recommendation:** ModelRouter — уніфікація та logging переважають

### Q2: Semantic matching алгоритм для alignment?

Варіанти:
- **Simple:** Regex extraction identifiers з code → fuzzy match в STT text
- **Embedding-based:** Embed VD scene summary + STT segment → cosine similarity
- **LLM-based:** LLM порівнює VD і STT для кожної пари

**Recommendation:** Simple regex + fuzzy match спочатку. Embedding якщо недостатньо.

### Q3: Resumability для довгих відео?

2-годинне відео = ~350 frames, ~100 scenes, ~350 Eyes calls. При 15 RPM = ~23 хвилини.
Якщо перервано на 50% — втрачаємо ~12 хвилин роботи.

**Options:**
- State file (як у spike) — просто, доведено
- DB persistence (EyesResult → таблиця) — складніше, але інтегровано

**Recommendation:** State file для MVP, DB persistence для production.

### Q4: Live coding відео?

Spike тестувався на слайд-відео. Live coding = постійні дрібні зміни → більше frames → більше Eyes calls.

**Mitigation:**
- Cooldown logic (вже в frame_sampler)
- dHash threshold може бути вищий для live coding scenes
- Scene memory agregує зміни → не потрібно описувати кожен keystroke

**Need:** Тест на live coding відео перед production.

### Q5: STT context в Eyes prompt — потрібен чи ні?

**Рішення:** Ні. VD і STT — незалежні потоки. VD має описувати те що БАЧИТЬ, а не те що лектор КАЖЕ. STT context може навіть зіпсувати VD — модель "побачить" те що чує, а не те що на екрані. Alignment вирішує mapping post-factum.
