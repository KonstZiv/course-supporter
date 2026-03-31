# Video Processing Pipeline — Повний план (Березень 2026)

**Дата:** 2026-03-31
**Статус:** Final — очікує результатів spike-досліджень
**Попередній spike:** [STT Spike Report](../stt-spike/STT-SPIKE-REPORT-UA.md) (2026-03-30)

---

## Зміст

1. [Контекст та мотивація](#1-контекст-та-мотивація)
2. [Реальні обсяги та cost budget](#2-реальні-обсяги-та-cost-budget)
3. [Архітектура високого рівня](#3-архітектура-високого-рівня)
4. [Stage A: Smart Frame Sampling](#4-stage-a-smart-frame-sampling)
5. [Stage B: Visual Analysis (two-pass)](#5-stage-b-visual-analysis-two-pass)
6. [Stage C: Structured OCR (умовний)](#6-stage-c-structured-ocr-умовний)
7. [Stage D: Aggregation Layer](#7-stage-d-aggregation-layer)
8. [Обробка довгих відео (chunking)](#8-обробка-довгих-відео-chunking)
9. [Інтеграція з існуючою архітектурою](#9-інтеграція-з-існуючою-архітектурою)
10. [Spike Plan](#10-spike-plan)
11. [Нові залежності](#11-нові-залежності)
12. [Порядок виконання](#12-порядок-виконання)
13. [Відкриті питання](#13-відкриті-питання)

---

## 1. Контекст та мотивація

### Поточний стан

Система `course-supporter` обробляє навчальні відео курсів програмування (Python, українською мовою) і перетворює їх на структуровані документи (`SourceDocument`).

**Audio track** — вирішено. STT spike (2026-03-30) встановив ланцюжок:
- **Primary:** ElevenLabs Scribe (найкраща загальна якість українською)
- **Secondary:** GPT-4o Mini Transcribe (найкраще збереження латинських tech-термінів)
- **Fallback:** Deepgram Nova-3 (найшвидший, 117x realtime)

Реалізація: `src/course_supporter/stt/` — `STTProvider` ABC, `STTRouter` з fallback chains, три провайдери.

**Video track** — не вирішено. Поточний `VideoProcessor` (`src/course_supporter/ingestion/video.py`) відправляє все відео до Gemini Vision і отримує транскрипцію. Це:
- Ігнорує візуальний контент (код на екрані, слайди, діаграми)
- Дає низькоякісну транскрипцію українською (суржик)
- Не розділяє audio та video streams

### Мета

Побудувати pipeline обробки відео-треку, який паралельно з STT витягує:
1. **Візуальні описи + текст** (VD) — що відбувається на екрані + точний текст/код з екрану
2. **Агрегацію** — об'єднання transcript + VD в єдину timeline з cross-references

---

## 2. Реальні обсяги та cost budget

### Типові курси

| Тип курсу | Кількість відео | Тривалість кожного | Загальний обсяг |
|---|---|---|---|
| Довгі лекції | 10 | ~2 год | **20 годин** |
| Середні лекції | 8 | 1-2 год | **8-16 годин** |
| Короткі уроки | 65 | 8-20 хв | **9-22 годин** |

**Висновок: 2-годинні відео — основний use case, не edge case.** Pipeline має обробляти їх як default.

### Очікувана кількість frames

| Тривалість | Scene detect | Після dHash | Pass 2 (~50%) |
|---|---|---|---|
| 10 хв | 80-150 | 50-100 | 25-50 |
| 1 год | 400-800 | 200-400 | 100-200 |
| 2 год | 800-1500 | 400-700 | 200-350 |

### Cost estimate (Gemini Flash, ElevenLabs STT)

| Курс | Годин | STT (~$0.004/хв) | VD (~$0.006/хв) | **Всього** |
|---|---|---|---|---|
| 10 × 2 год | 20 | $4.80 | $7.20 | **~$12** |
| 8 × 1.5 год | 12 | $2.90 | $4.30 | **~$7** |
| 65 × 14 хв | 15 | $3.60 | $5.40 | **~$9** |
| **Разом** | **47 год** | **$11.30** | **$16.90** | **~$28** |

### Processing time estimate (2-год відео)

| Етап | Час | Примітка |
|---|---|---|
| Frame extraction (scene detect + dHash) | ~2-3 хв | FFmpeg hardware-accelerated |
| STT (ElevenLabs, паралельно) | ~5 хв | 24x realtime |
| Pass 1 classification | ~1-2 хв | Parallel batches, Gemini Flash |
| Pass 2 detailed analysis | ~2-3 хв | Parallel batches |
| **Total** | **~10-15 хв** | На 2-годинне відео |

---

## 3. Архітектура високого рівня

### Ключове рішення: Vision LLM = VD + OCR в одному запиті

Сучасні Vision LLM (Gemini Flash, GPT-4o, Claude) вже читають текст з зображень з високою точністю. Замість двох окремих кроків — **один запит** який дає і опис, і extracted text:

```
Два окремі кроки (не обираємо):
  Frame → Vision LLM ($) → description
  Frame → OCR engine → raw text → LLM ($) → corrected text
  = 2+ виклики per frame

Unified (обраний підхід):
  Frame → Vision LLM ($) → description + extracted text + code
  = 1 виклик per frame, ~50% дешевше
```

**Stage C (окремий OCR) стає умовним** — виконується тільки якщо Spike B покаже що Vision LLM не дає >90% accuracy для Python коду.

### Два підходи до обробки (STT context)

STT через Deepgram = ~15 сек для 10-хв відео. Frame extraction + dHash = ~30 сек. Transcript готовий раніше ніж починається Vision LLM:

```
Підхід A: Повністю паралельно (простіше)
  audio → STT ────────────────────→ transcript
  video → frames → Vision LLM ──→ descriptions
  ──────────────────────────────→ merge

Підхід B: Semi-sequential (якісніше, Spike B визначає)
  audio → STT → transcript ─────────────────────────────┐
  video → frames ─────┐                                  │
                      ├→ Vision LLM (з контекстом STT) ──┤
                      │  "лектор зараз говорить про X"    │
                      │  → краща інтерпретація коду       │
                      └───────────────────────────────────→ merge
```

Spike B перевірить: чи STT контекст суттєво покращує якість Vision LLM описів.

### Архітектура

```
Video file
├── Audio track → [chunking якщо >25MB] → STT chain
│   └── STTResult.segments[] → ContentChunk(TRANSCRIPT)
│
└── Video track
    │
    ├── Stage A: Smart Frame Sampling
    │   scene_detect (FFmpeg) → dHash dedup → PiP tracking → cooldown → unique frames
    │
    ├── Stage B: Visual Analysis (two-pass Vision LLM)
    │   Pass 1 (batch, cheap): classification + short descriptions → filter
    │   Pass 2 (selective, quality, parallel batches): detailed description + text/code
    │
    ├── Stage C: Structured OCR [УМОВНИЙ — якщо Vision LLM < 90% code accuracy]
    │   OCR engine → LLM correction
    │
    └── Stage D: Aggregation Layer
        Merge by timestamps → cross-reference → deduplicate
        → SourceDocument (unified timeline)
```

### Потік даних

```python
VideoProcessor.process(source)
    ├── # Phase 1: Extract + STT (паралельно з frame sampling)
    │   audio_path = extract_audio(video_path)
    │   audio_chunks = chunk_audio_if_needed(audio_path)  # >25MB → split
    │   stt_task = stt_router.transcribe(audio_chunks)
    │   frames = frame_sampler.extract(video_path)
    │
    ├── stt_result = await stt_task  # transcript ready
    │
    ├── # Phase 2: Visual analysis (з контекстом STT якщо підхід B)
    │   vd_result = await vd_pipeline.process(frames, transcript=stt_result)
    │
    └── # Phase 3: Aggregation
        merged = aggregation.merge(stt_result, vd_result)
        → SourceDocument
```

---

## 4. Stage A: Smart Frame Sampling

### Мета

З відео тривалістю 10 хв — 2 год витягти 50-700 "унікальних" кадрів, що фіксують зміни контенту.

### Алгоритм

#### 4.1. Pre-filter: FFmpeg scene detection FIRST

Замість витягування всіх кадрів з фіксованим fps → спочатку scene detection, потім fallback interval:

```bash
# Крок 1: Scene change detection — витягує тільки кадри де картинка змінилась
ffmpeg -i video.mp4 -vf "select=gt(scene,0.3)" -vsync vfr frames/scene_%04d.jpg

# Крок 2: Fallback minimum interval — щоб не пропустити повільні зміни
ffmpeg -i video.mp4 -vf fps=0.5 -q:v 2 frames/interval_%04d.jpg
```

**Комбінована стратегія:** scene detection + мінімальний інтервал (не рідше 1 кадр / 30 сек):

```python
def extract_frames(video_path: Path, min_interval_sec: float = 30.0) -> list[RawFrame]:
    # 1. Scene detection
    scene_frames = ffmpeg_scene_detect(video_path, threshold=0.3)

    # 2. Заповнити gaps — якщо між двома scene frames > min_interval_sec
    interval_frames = ffmpeg_fps_extract(video_path, fps=1.0/min_interval_sec)

    # 3. Merge + deduplicate by timestamp (±1 sec tolerance)
    all_frames = merge_by_timestamp(scene_frames, interval_frames, tolerance_sec=1.0)

    return all_frames  # ~100-200 кадрів для 10 хв, ~800-1500 для 2 год
```

**Мінімальна роздільність:** Для надійного читання коду Vision LLM потребує достатню роздільність. Якщо вихідне відео < 720p — попередження в логах, crop strategy (Stage B) стає критичнішим.

```python
MIN_FRAME_WIDTH = 1280  # мінімум 720p width
```

#### 4.2. PiP Camera — Dynamic Tracking

Лектор може переміщувати PiP камеру протягом відео. Деякі відео — чисті screen recordings без PiP.

**Трирівневий підхід:**

**Рівень 1 — Initial detection (один раз):**

Перші 5-10 кадрів → Vision LLM: "Де PiP камера лектора? Дай bounding box координати у відсотках. Якщо камери немає — `none`." → базова маска або `no_pip` mode.

**Рівень 2 — Lightweight tracking (per checkpoint, дешевий):**

PiP камера = зона з постійним motion (обличчя, жести) серед статичного контенту. Детектимо через temporal diff без LLM:

```python
def track_pip(
    frame_prev: np.ndarray,
    frame_curr: np.ndarray,
    current_mask: PiPMask,
    candidate_zones: dict[str, Rect],  # 4 кути + центри сторін
) -> PiPMask:
    """Lightweight PiP tracking via temporal motion detection.

    PiP zone = area with highest pixel change between consecutive frames
    (lecturer always moves — talks, gestures), while slides/code are static.
    """
    diff = cv2.absdiff(frame_prev, frame_curr)

    zone_motion: dict[str, float] = {}
    for name, rect in candidate_zones.items():
        zone_diff = diff[rect.y1:rect.y2, rect.x1:rect.x2]
        zone_motion[name] = float(np.mean(zone_diff))

    # PiP = zone with highest motion
    best_zone = max(zone_motion, key=zone_motion.get)

    # Confidence: якщо motion в best_zone значно вищий за інші — PiP точно там
    motion_values = sorted(zone_motion.values(), reverse=True)
    confidence = (motion_values[0] - motion_values[1]) / (motion_values[0] + 1e-6)

    if best_zone != current_mask.zone_name and confidence > 0.3:
        return PiPMask(zone_name=best_zone, rect=candidate_zones[best_zone])

    return current_mask
```

**Рівень 3 — Validation / re-detection (рідко):**

Кожні N кадрів (кожні 30 секунд) — перевірити маску:
- Motion переїхала → оновити маску (auto)
- Motion впала до нуля в PiP зоні → PiP вимкнена → прибрати маску (auto)
- Невпевнений (confidence < 0.3) → одноразовий fallback Vision LLM: "де PiP зараз?"

**No-PiP handling:** Якщо initial detection повернув `"none"` → Рівні 2-3 працюють в passive mode: перевіряють чи PiP не з'явилась (motion в одному з кутів значно вище інших). Якщо ні — dHash працює на повному кадрі без маскування.

**Cost:** temporal diff = numpy, $0. Vision LLM fallback = 1-2 рази за відео максимум.

**PiP position log:**

```python
class PiPEvent(BaseModel):
    timestamp_sec: float
    zone_name: str          # "bottom_right", "top_left", "none", etc.
    rect: Rect | None       # bounding box
    detection_method: str   # "vision_llm", "motion_tracking", "disappeared"
    confidence: float
```

**Реалізація маскування:** При обчисленні dHash — замінюємо пікселі PiP зони на константу (сірий), щоб PiP-зміни не впливали на hash.

#### 4.3. dHash (Difference Hash)

- **Що:** Perceptual hash, який порівнює gradient (різницю яскравості) сусідніх пікселів
- **Чому dHash:** Стійкіший за aHash до незначних змін яскравості; менш чутливий за pixel diff до шуму/артефактів стиснення
- **Реалізація:** бібліотека `imagehash` (Python): `imagehash.dhash(image, hash_size=16)`
- **Порівняння:** Hamming distance між хешами послідовних кадрів

```python
import imagehash
from PIL import Image

hash1 = imagehash.dhash(Image.open("frame_001.jpg"), hash_size=16)
hash2 = imagehash.dhash(Image.open("frame_002.jpg"), hash_size=16)
distance = hash1 - hash2  # Hamming distance (0 = identical)

# Для hash_size=16: max distance = 256
# threshold ~10% = 25-26 → "новий кадр"
is_new_frame = distance > DHASH_THRESHOLD
```

**Пороги для tuning під час spike:**

| Threshold (% від max) | Hamming distance (hash_size=16) | Очікуваний ефект |
|---|---|---|
| 5% | ~13 | Агресивна дедуплікація — тільки великі зміни |
| 10% | ~25 | Баланс — нові слайди, суттєві зміни коду |
| 15% | ~38 | Більше кадрів — включаючи дрібні зміни |
| 20% | ~51 | Мінімальна дедуплікація |

**ВАЖЛИВО:** dHash обчислюється ПІСЛЯ маскування PiP зони.

#### 4.4. Cooldown Logic (для live coding)

При набиранні коду в IDE — постійні дрібні зміни кожну секунду. Без cooldown отримаємо 100+ кадрів друку однієї функції.

**Алгоритм:**

```
if frames_are_changing_continuously:
    wait for COOLDOWN_SEC seconds of stability
    then capture "completed" frame
else:
    capture frame immediately when change detected
```

**Евристика "continuous change":**
- Якщо 3+ послідовних кадрів мають Hamming distance > threshold → mode = "coding"
- У режимі "coding": чекаємо `COOLDOWN_SEC` (3-5 секунд) стабільності перед capture
- Стабільність = N послідовних кадрів з distance < threshold

#### 4.5. Результат Stage A

```python
class SampledFrame(BaseModel):
    """Single unique frame extracted from video."""
    frame_index: int          # порядковий номер у filtered set
    timestamp_sec: float      # час у відео
    image_path: str           # шлях до зображення
    dhash: str                # hex-encoded dHash
    hamming_from_prev: int    # відстань від попереднього frame
    pip_mask: PiPMask | None  # поточна PiP маска для цього кадру

class FrameSamplingResult(BaseModel):
    """Complete output of Stage A."""
    frames: list[SampledFrame]
    pip_events: list[PiPEvent]      # PiP position changes log
    total_raw_frames: int           # скільки було до фільтрації
    sampling_params: dict           # threshold, hash_size, cooldown_sec
    video_resolution: tuple[int, int]  # (width, height) — для перевірки мінімальної роздільності
```

**Примітка:** `scene_type` (slide/code/terminal/talking_head) **не визначається в Stage A** — це робить Stage B Pass 1 (Vision LLM classification). Stage A тільки витягує унікальні кадри.

---

## 5. Stage B: Visual Analysis (two-pass)

### Мета

Для набору filtered frames отримати: (1) класифікацію сцени, (2) текстовий опис, (3) extracted text/code. Two-pass підхід оптимізує cost — детальний аналіз тільки для важливих кадрів.

### 5.1. Pass 1: Classification + Short Description (batch, cheap)

**Мета:** Класифікувати кожен кадр і дати короткий опис. Відфільтрувати talking_head і прості transitions.

Відправляємо **batch 20-30 кадрів** до Vision LLM (Gemini Flash — найдешевший, великий контекст):

```
You are analyzing frames from a Ukrainian programming course video.
Frames are chronologically ordered.

For each frame, provide:
1. scene_type: one of [slide, code_editing, terminal, diagram, transition, talking_head]
2. short_description: 1-2 sentences about what's on screen
3. has_text_content: true/false — is there meaningful text/code to extract?
4. importance: 1-5 (5 = critical content, 1 = can skip)

Respond as JSON array.
```

**Результат Pass 1:**

```python
class FrameClassification(BaseModel):
    frame_index: int
    scene_type: str           # "slide" | "code_editing" | "terminal" | "diagram" | "transition" | "talking_head"
    short_description: str    # 1-2 речення
    has_text_content: bool    # чи є текст/код для extraction
    importance: int           # 1-5
```

**Фільтрація:** Pass 2 отримує тільки кадри де `importance >= 3` або `has_text_content == True`. Talking_head і прості transitions скіпаються.

**Cost Pass 1:** ~$0.001-0.003 per batch of 20 frames (Gemini Flash).

### 5.2. Pass 2: Detailed Analysis + Text Extraction (selective, quality)

**Мета:** Для відфільтрованих кадрів — детальний опис + точний текст/код.

**Ключове рішення:** Vision LLM робить VD + OCR **в одному запиті**:

```
You are analyzing frames from a Ukrainian programming course video.
The lecturer is currently saying: "{stt_context_if_available}"

For each frame, provide:
1. detailed_description: What is happening on screen. Focus on content changes,
   new material appearing, transitions. Language: Ukrainian.
2. extracted_text: Exact text visible on screen. For code — preserve indentation,
   underscores, arrows (->). Format:
   - Code: wrap in ```python ... ```
   - Slide text: use # for headings, - for bullets
   - Terminal: wrap in ```bash ... ```
   - If no meaningful text: null

CRITICAL for code extraction:
- Preserve Python indentation exactly
- __init__, __str__ — double underscores
- -> for return type annotations
- Capture ALL visible code, not just highlights
```

**Моделі для Pass 2:**

| Model | Коли використовувати | Cost estimate |
|---|---|---|
| **Gemini 2.5 Flash** | Default — дешевий, великий контекст | ~$0.003 per batch |
| **Gemini 2.5 Pro** | Складні діаграми, дрібний текст | ~$0.024 per batch |
| **GPT-4o** | Fallback, добре читає код | ~$0.015 per batch |
| **Claude Sonnet** | Деталізований опис, good reasoning | ~$0.015 per batch |

**STT контекст (підхід B):** Якщо STT transcript вже доступний — включити в промпт 2-3 речення що лектор говорить в цей момент. Spike B визначить чи це суттєво покращує якість.

**Паралельність Pass 2:** Batches незалежні → обробляємо через `asyncio.gather`. Для 2-год відео з 350 Pass 2 frames (35 batches по 10) — 5-7 паралельних запитів значно прискорять обробку.

### 5.3. Progressive Detail Level

Не всі кадри потребують однакової глибини аналізу:

| Scene type | Pass 2 depth | Text extraction | Approx cost |
|---|---|---|---|
| talking_head | **skip** (Pass 1 description достатній) | Ні | $0 |
| transition | **skip** або "перехід до [target]" | Ні | мінімальний |
| slide | Повний опис + текст | Так | середній |
| code_editing | Повний опис + **exact code** | Так, з увагою на indentation | максимальний |
| terminal | Опис + command/output | Так | середній |
| diagram | Повний опис + labels | Labels тільки | середній |

**Результат:** ~50% кадрів скіпаються в Pass 2 → вдвічі менший cost.

### 5.4. Crop Strategy замість Agentic Zoom

Rule-based crop — простіше, передбачуваніше, без додаткових LLM-викликів:

```python
def smart_crop(frame: Image, scene_type: str) -> list[Image]:
    """Pre-crop frame based on scene type for better text recognition."""
    crops = [frame]  # always include full frame

    if scene_type == "code_editing":
        # IDE: код зазвичай в лівих 70% екрану, PiP справа
        crops.append(frame.crop(left=0, right=0.7, top=0.05, bottom=0.95))
    elif scene_type == "slide":
        # Slide: центральна частина
        crops.append(frame.crop(left=0.05, right=0.95, top=0.05, bottom=0.9))
    elif scene_type == "terminal":
        # Terminal: може бути full screen або частина
        crops.append(frame.crop(left=0, right=1.0, top=0.3, bottom=1.0))

    return crops
```

Crop відправляється в Pass 2 разом з full frame — Vision LLM отримує і контекст, і деталі.

### 5.5. Результат Stage B

```python
class VisualAnalysis(BaseModel):
    """Complete visual analysis of a frame or frame group."""
    frame_range: tuple[int, int]      # (start_idx, end_idx) of SampledFrames
    timestamp_range: tuple[float, float]  # (start_sec, end_sec)
    scene_type: str                   # from Pass 1 classification
    description: str                  # detailed description (Pass 2, або short з Pass 1)
    extracted_text: str | None        # exact text/code from screen (Pass 2)
    text_type: str | None             # "python" | "bash" | "slide_text" | "diagram_label"
    importance: int                   # 1-5
    confidence: float | None = None
    stt_context_used: bool = False    # чи був STT контекст в промпті
```

---

## 6. Stage C: Structured OCR (умовний)

### УВАГА: Stage C виконується ТІЛЬКИ якщо Spike B покаже що Vision LLM не дає >90% accuracy для Python коду

Якщо Vision LLM достатній (очікуваний сценарій) — Stage C **не реалізується**. Text extraction відбувається в Stage B Pass 2.

### Коли Stage C потрібен

| Сценарій | Stage C? |
|---|---|
| Vision LLM дає >90% code accuracy | **Ні** — Stage B достатній |
| Vision LLM дає 70-90% code accuracy | **Так, як fallback** — OCR + LLM correction для code frames |
| Vision LLM дає <70% code accuracy | **Так, як primary** — OCR для всіх text frames |

### 6.1. OCR Engine (визначається Spike C, якщо потрібен)

| Engine | Сильні сторони | Слабкі сторони |
|---|---|---|
| **Surya OCR** | SOTA для code recognition (2025-2026), layout detection | Нова бібліотека, може бути нестабільною |
| **PaddleOCR** | Зрілий проект, multilingual, layout analysis | Важка залежність (PaddlePaddle) |
| **Tesseract** | Безкоштовний, широко доступний | Погано з dark themes IDE |
| **Google Vision API** | Висока якість, cloud-based | Платний, network latency |

### 6.2. LLM Post-processing (OCR Correction)

OCR часто помиляється на: `_` → `-`, `->` → `>`, `__init__` → `_init_`, indentation.

**Рішення:** LLM-агент з контекстом Stage B виправляє OCR-помилки:

```
You are correcting OCR output from a Python programming course video.
The lecturer is explaining: {visual_description_from_stage_b}

OCR extracted this code:
{raw_ocr_text}

Fix common OCR errors: underscores, arrows, indentation, Python keywords.
Return corrected text in markdown format.
```

Модель: DeepSeek Chat або GPT-4o Mini (дешевий, не потребує vision).

### 6.3. Результат Stage C

```python
class OCRExtraction(BaseModel):
    """Structured text extracted via OCR (only if Stage C active)."""
    frame_index: int
    timestamp_sec: float
    raw_text: str          # оригінальний OCR output
    corrected_text: str    # після LLM correction
    text_type: str         # "code" | "slide_text" | "terminal" | "diagram_label"
    language_tag: str | None  # "python", "bash", None
    confidence: float | None
```

---

## 7. Stage D: Aggregation Layer

### Мета

Об'єднати потоки (STT transcript, VD descriptions, опціонально OCR) в єдину timeline → `SourceDocument` з cross-references і дедуплікацією.

### 7.1. Merged Timeline (приклад)

| Timestamp | Source | ChunkType | Content |
|---|---|---|---|
| 02:15 | STT | `TRANSCRIPT` | "Тепер давайте подивимось на структуру нашої моделі..." |
| 02:17 | VD | `VISUAL_DESCRIPTION` | Перехід від лектора до VS Code. Файл models.py відкритий. |
| 02:20 | VD | `CODE_BLOCK` | ````python\nclass Model:\n    def __init__(self):\n```` |
| 02:35 | STT | `TRANSCRIPT` | "Як бачите, ми визначаємо клас Model з конструктором..." |
| 02:38 | VD | `CODE_BLOCK` | ````python\n        self.name = name\n```` |
| 03:00 | VD | `VISUAL_DESCRIPTION` | Перехід на слайд "Архітектура MVC". Діаграма. |
| 03:02 | VD | `SLIDE_OCR` | "Архітектура MVC: Model — View — Controller" |

### 7.2. Алгоритм агрегації

```python
class AggregationLayer:
    def merge(
        self,
        stt_result: TranscriptResult,
        vd_result: VisualAnalysisResult,
        ocr_chunks: list[ContentChunk] | None = None,  # тільки якщо Stage C active
    ) -> list[ContentChunk]:
        """Merge all streams with cross-referencing and deduplication."""

        transcript_chunks = self._stt_to_chunks(stt_result)
        visual_chunks = self._vd_to_chunks(vd_result)

        all_chunks = transcript_chunks + visual_chunks
        if ocr_chunks:
            all_chunks += ocr_chunks

        # Step 1: Sort by timestamp
        all_chunks.sort(key=lambda c: c.metadata.get("start_sec", 0))

        # Step 2: Cross-reference — link related chunks
        self._cross_reference(all_chunks)

        # Step 3: Deduplicate overlapping content
        all_chunks = self._deduplicate(all_chunks)

        # Step 4: Assign sequential index
        for i, chunk in enumerate(all_chunks):
            chunk.index = i

        return all_chunks

    def _cross_reference(self, chunks: list[ContentChunk]) -> None:
        """Link transcript chunks with visual chunks at same timestamp.

        Якщо STT каже "давайте подивимось на клас Model" і VD показує
        код з class Model — ці chunks пов'язані. Зберігаємо reference
        в metadata для downstream agents.
        """
        # Window-based: для кожного VD chunk знайти STT chunks ±5 сек
        ...

    def _deduplicate(self, chunks: list[ContentChunk]) -> list[ContentChunk]:
        """Remove redundant chunks.

        Наприклад: якщо Stage B extracted_text і Stage C OCR дали
        однаковий код — залишити один (з вищою confidence).
        """
        ...
```

### 7.3. Порядок chunks з однаковим timestamp

| Priority | ChunkType | Причина |
|---|---|---|
| 1 | `VISUAL_DESCRIPTION` | Контекст — що відбувається на екрані |
| 2 | `CODE_BLOCK` / `SLIDE_OCR` | Точний контент |
| 3 | `TRANSCRIPT` | Що лектор каже |

### 7.4. Нові ChunkType значення

Розширення `ChunkType` enum у `src/course_supporter/models/source.py`:

```python
class ChunkType(StrEnum):
    # Існуючі
    TRANSCRIPT = "transcript"
    SLIDE_TEXT = "slide_text"
    SLIDE_DESCRIPTION = "slide_description"
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    WEB_CONTENT = "web_content"
    METADATA = "metadata"
    # Нові для VD pipeline
    VISUAL_DESCRIPTION = "visual_description"    # Stage B: що відбувається на екрані
    CODE_BLOCK = "code_block"                    # Stage B/C: код з екрану
    SLIDE_OCR = "slide_ocr"                      # Stage B/C: текст слайду
    TERMINAL_OUTPUT = "terminal_output"           # Stage B/C: вивід terminal
    DIAGRAM_DESCRIPTION = "diagram_description"   # Stage B: опис діаграми
```

---

## 8. Обробка довгих відео (chunking)

### 8.1. Audio chunking (обов'язковий)

2 години MP3 при 64kbps ≈ 58 MB. Ліміти STT API:

| Provider | Max file size | 2-год файл проходить? |
|---|---|---|
| ElevenLabs | 1 GB | Так |
| Deepgram | 2 GB | Так |
| OpenAI (GPT-4o Mini, Whisper) | **25 MB** | **Ні — потрібен chunking** |

**Рішення:** Для OpenAI fallback — різати аудіо на сегменти ~10-15 хв через FFmpeg:

```python
async def chunk_audio_if_needed(
    audio_path: Path, max_size_mb: float = 24.0
) -> list[Path]:
    """Split audio into chunks if file exceeds STT API limits."""
    size_mb = audio_path.stat().st_size / (1024 * 1024)
    if size_mb <= max_size_mb:
        return [audio_path]

    # Estimate segment duration to stay under limit
    duration_sec = await get_audio_duration(audio_path)
    segments_needed = math.ceil(size_mb / max_size_mb)
    segment_duration = duration_sec / segments_needed

    return await ffmpeg_split_audio(audio_path, segment_duration_sec=segment_duration)
```

**Merge:** STT результати з сегментів merge-аться з коригуванням timestamps (segment offset).

### 8.2. Video frame processing (потокова обробка)

FFmpeg scene detection працює потоково — не тримає все відео в пам'яті. Для 2-год відео:

```
FFmpeg scene detect → stream кадрів → dHash online → filtered frames (temp dir)
```

**PiP state:** Передається між кадрами як mutable state — initial detection на перших кадрах, tracking на решті.

### 8.3. Vision LLM batching для довгих відео

Для 2-год відео з ~350 Pass 2 frames:
- **Pass 1:** 700 frames ÷ 30 per batch = ~23 batches → `asyncio.gather` з обмеженням concurrency (5-7 одночасних запитів)
- **Pass 2:** 350 frames ÷ 10 per batch = ~35 batches → `asyncio.gather` з тим же concurrency limit

```python
VISION_LLM_CONCURRENCY = 5  # max parallel Vision LLM requests

async def process_batches(batches: list[Batch], concurrency: int) -> list[Result]:
    semaphore = asyncio.Semaphore(concurrency)
    async def limited(batch):
        async with semaphore:
            return await process_single_batch(batch)
    return await asyncio.gather(*[limited(b) for b in batches])
```

### 8.4. Batch processing курсу

Для курсу з 10-65 відео — обробка через ARQ worker queue (вже є в проекті):
- Один ARQ worker обробляє відео послідовно
- Можна запустити 2-3 workers паралельно для прискорення
- Кожне відео — окрема ARQ task з progress tracking

---

## 9. Інтеграція з існуючою архітектурою

### 9.1. Повторне використання паттернів

| Паттерн | STT приклад | VD аналог |
|---|---|---|
| **Provider ABC** | `STTProvider` (`stt/providers/base.py`) | Не потрібен окремий — використовуємо `ModelRouter` |
| **Router + fallback** | `STTRouter` (`stt/router.py`) | Існуючий `ModelRouter` (вже підтримує vision) |
| **Factory** | `create_stt_providers()` (`stt/factory.py`) | `create_vd_pipeline()` |
| **Registry YAML** | `external_services.yaml` actions | Нові actions: `visual_classification`, `visual_analysis`, `ocr_correction` |
| **Heavy steps** | `TranscribeFunc` protocol | `FrameSampleFunc` protocol |

### 9.2. VD через існуючий ModelRouter

Vision LLM виклики — це ті самі LLM completions з `contents=[images]`, тому **використовуємо існуючий `ModelRouter`** з новими actions:

```yaml
# Доповнення до config/external_services.yaml
actions:
  visual_classification:
    requires: [vision]
    chain:
      default: [gemini-2.5-flash]           # Pass 1 — тільки дешева модель
      budget: [gemini-2.5-flash]

  visual_analysis:
    requires: [vision]
    chain:
      default: [gemini-2.5-flash, gpt-4o]   # Pass 2 — quality з fallback
      quality: [gemini-2.5-pro, gemini-2.5-flash]
      budget: [gemini-2.5-flash]

  ocr_correction:                            # тільки якщо Stage C active
    requires: [structured_output]
    chain:
      default: [deepseek-chat, gemini-2.5-flash]
      quality: [gemini-2.5-flash, claude-sonnet]
      budget: [deepseek-chat]
```

### 9.3. Новий VideoProcessor (redesign)

```python
# src/course_supporter/ingestion/video.py (redesigned)
class VideoProcessor(SourceProcessor):
    """Parallel STT + VD video processing."""

    def __init__(
        self,
        stt_router: STTRouter,
        vd_pipeline: VDPipeline,
        aggregation: AggregationLayer,
    ) -> None:
        self._stt = stt_router
        self._vd = vd_pipeline
        self._aggregation = aggregation

    async def process(self, source: MaterialEntry, *, router=None) -> SourceDocument:
        video_path = await self._download(source.source_url)
        audio_path = await self._extract_audio(video_path)

        # Phase 1: STT паралельно з frame extraction
        stt_task = asyncio.create_task(
            self._stt.transcribe("transcribe", audio_path)
        )
        frames = await self._vd.extract_frames(video_path)
        stt_result = await stt_task  # transcript ready before Vision LLM

        # Phase 2: Visual analysis (з STT контекстом якщо підхід B)
        vd_result = await self._vd.analyze(frames, transcript=stt_result)

        # Phase 3: Aggregation
        merged = self._aggregation.merge(stt_result, vd_result)

        return SourceDocument(
            source_type=SourceType.VIDEO,
            source_url=source.source_url,
            title=source.filename or "",
            chunks=merged,
            metadata={
                "strategy": "stt+vd",
                "stt_provider": stt_result.provider,
                "vd_frames_total": len(frames.frames),
                "vd_frames_analyzed": vd_result.frames_analyzed,
                "pip_events": len(frames.pip_events),
            },
        )
```

### 9.4. Структура директорії

```
src/course_supporter/
├── vd/                         # NEW: Video Description module
│   ├── __init__.py
│   ├── pipeline.py             # VDPipeline: orchestrates A→B(→C)
│   ├── frame_sampler.py        # Stage A: scene_detect + dHash + cooldown
│   ├── pip_tracker.py          # PiP detection + dynamic tracking
│   ├── visual_analyzer.py      # Stage B: two-pass Vision LLM (classification + analysis)
│   ├── ocr_extractor.py        # Stage C: OCR + LLM correction [УМОВНИЙ]
│   ├── aggregation.py          # Stage D: merge + cross-reference + deduplicate
│   └── schemas.py              # SampledFrame, VisualAnalysis, OCRExtraction, PiPEvent
├── ingestion/
│   └── video.py                # MODIFIED: new VideoProcessor using STT + VD
```

### 9.5. Cleanup тимчасових файлів

Frame extraction створює ~100-700 JPEG файлів (~10-70MB для 2-год відео). Cleanup обов'язковий:

```python
class VDPipeline:
    async def process(self, video_path: Path, transcript=None) -> VDResult:
        temp_dir = Path(tempfile.mkdtemp(prefix="vd_frames_"))
        try:
            frames = await self._sampler.extract(video_path, output_dir=temp_dir)
            result = await self._analyzer.analyze(frames, transcript=transcript)
            return result
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
```

---

## 10. Spike Plan

Усі spike використовують **те саме відео**, що й STT spike:
- **YouTube:** https://www.youtube.com/watch?v=bRYsA9Yyvy4
- **Відео:** "2.01 Python Quick Start: Datatype — what it is?"
- **Тривалість:** 10 хв 35 сек

### Spike A: Frame Sampling + PiP Tracking

**Мета:** Визначити оптимальні параметри frame sampling та PiP tracking.

**Задачі:**
1. Завантажити відео, витягти кадри:
   - FFmpeg scene detection (`select=gt(scene,0.3)`) — порахувати скільки кадрів
   - FFmpeg fps=0.5 (кожні 2 сек) — порахувати
   - Комбінована стратегія (scene + min interval 30 сек)
2. dHash з різними `hash_size` (8, 12, 16) і thresholds (5%, 10%, 15%, 20%)
3. PiP tracking:
   - Vision LLM initial detection на перших 10 кадрах
   - Temporal diff tracking — чи стабільно тримає позицію?
   - Тест: якщо PiP переміщується — чи tracking це ловить?
   - Порівняти: з PiP маскою vs без → різниця в dHash false positives
4. Cooldown logic — порівняти з cooldown vs без (якщо є live coding у відео)
5. Порахувати кількість фінальних кадрів для кожної комбінації параметрів
6. Перевірити роздільність — чи достатня для читання коду Vision LLM

**Інструменти:** `ffmpeg`, `imagehash`, `opencv-python-headless`, `Pillow`, `numpy`

**Deliverables:**
- Скрипт `scripts/spike_frame_sampling.py`
- Таблиця: (scene_detect vs fps) × threshold × hash_size × PiP mask → кількість кадрів
- PiP tracking log: чи зміщувалась PiP, як tracking реагував
- Набір "golden" кадрів (50-200 шт.) для Spike B
- Рекомендовані параметри для production

**Критерії успіху:**
- Слайд-переходи завжди фіксуються (recall 100%)
- Scene detection + dHash дає 50-200 кадрів для 10-хв відео
- PiP tracking стабільний — не генерує false positive зміни
- Друк коду не генерує 100+ near-identical кадрів

### Spike B: Vision LLM (розширений)

**Мета:** Порівняти якість Vision LLM для (1) описів, (2) text/code extraction, (3) combined prompt. Визначити чи потрібен Stage C.

**Передумова:** Golden frames з Spike A.

**Задачі:**
1. Обрати 20-30 representative frames з різних сцен:
   - 5 слайдів (текст, діаграма, код на слайді)
   - 5 кадрів live coding (різні стадії набору, dark IDE)
   - 5 terminal output
   - 5 переходів (між слайдами, між слайдом і IDE)
   - 5 talking head (контрольна група)
2. **Тест 1: Описи (batch)** — batch 10-20 frames з промптом "describe what happens":
   - Gemini 2.5 Flash, Gemini 2.5 Pro, GPT-4o, Claude Sonnet
3. **Тест 2: Vision LLM як OCR** — промпт "extract exact code/text from screen":
   - Ті ж моделі
   - Ground truth: вручну переписати код з 5 IDE кадрів і 5 slide кадрів
   - Порахувати character accuracy і code accuracy (indentation, underscores, arrows)
4. **Тест 3: Combined prompt** — "describe AND extract text" в одному запиті:
   - Порівняти якість vs окремі запити
   - Порівняти cost
5. **Тест 4: STT context** — додати transcript context до промпту:
   - Порівняти якість описів з контекстом vs без
   - Визначити чи підхід B (semi-sequential) виправданий
6. **Тест 5: Two-pass** — Pass 1 classification → filter → Pass 2 detailed:
   - Порахувати скільки кадрів скіпається
   - Порахувати total cost two-pass vs one-pass
7. Тест crop strategy: full frame vs cropped → різниця в code accuracy

**Deliverables:**
- Скрипт `scripts/spike_vision_llm.py`
- Ground truth файли для 10 кадрів (5 IDE + 5 slides)
- Таблиця: provider × scene_type → якість опису (1-5), code accuracy (%), latency, cost
- Таблиця: separate VD+OCR vs combined prompt → якість, cost
- Таблиця: with STT context vs without → якість
- **КЛЮЧОВЕ РІШЕННЯ:** Чи потрібен Stage C?
  - Vision LLM code accuracy >90% → Stage C НЕ потрібен
  - Vision LLM code accuracy 70-90% → Stage C як fallback для code frames
  - Vision LLM code accuracy <70% → Stage C обов'язковий
- Рекомендація: primary → fallback chain, one-pass vs two-pass

**Критерії успіху:**
- Описи слайдів містять ключовий зміст
- Описи коду вказують мову, назву функції/класу
- Code extraction: Python indentation збережений, `__init__` правильний
- Визначено: потрібен Stage C чи ні
- Combined prompt не гірший за окремі (якість) і дешевший (cost)

### Spike C: OCR Accuracy (УМОВНИЙ)

**Виконується ТІЛЬКИ якщо Spike B показав що Vision LLM code accuracy < 90%.**

**Мета:** Порівняти точність OCR engines на скріншотах IDE (dark theme) та слайдах.

**Передумова:** Golden frames з Spike A, ground truth з Spike B.

**Задачі:**
1. Протестувати OCR engines на тих самих 10 кадрах з ground truth:
   - Surya OCR, PaddleOCR, Tesseract, Google Vision API
2. Порівняти: raw accuracy vs accuracy після LLM post-processing
3. Порівняти: OCR + LLM correction vs Vision LLM (Spike B) → який краще для коду?
4. Тестувати pre-processing: invert colors для dark IDE themes

**Deliverables:**
- Скрипт `scripts/spike_ocr.py`
- Таблиця: engine × content_type → character accuracy (%), code accuracy (%)
- Таблиця: raw OCR vs LLM-corrected vs Vision LLM (Spike B)
- Рекомендація: який OCR engine для production (якщо потрібен)

---

## 11. Нові залежності

### Обов'язкові (Stage A)

```toml
# pyproject.toml additions
[project.optional-dependencies]
vd = [
    "opencv-python-headless>=4.9",  # Frame extraction, masking, temporal diff
    "Pillow>=10.0",                 # Image manipulation
    "ImageHash>=4.3",               # Perceptual hashing (dHash)
]
```

### За результатами spike (Stage C — тільки якщо потрібен)

```toml
vd-ocr = [
    "surya-ocr>=0.6",     # Якщо Spike C обере Surya
    # АБО
    "paddleocr>=2.8",     # Якщо Spike C обере PaddleOCR
    # АБО
    "pytesseract>=0.3",   # Якщо Spike C обере Tesseract
]
```

### mypy overrides

```toml
[[tool.mypy.overrides]]
module = "cv2.*"
ignore_missing_imports = true

[[tool.mypy.overrides]]
module = "imagehash.*"
ignore_missing_imports = true
```

---

## 12. Порядок виконання

```
Фаза 0: Підготовка
├── Створити current-doc/vd-spike/
├── Додати залежності: opencv, Pillow, imagehash
└── Завантажити тестове відео (yt-dlp)

Фаза 1: Spike A — Frame Sampling + PiP Tracking
├── Scene detection vs fps extraction
├── dHash tuning (threshold, hash_size)
├── PiP: initial detection + temporal diff tracking
├── Cooldown logic
├── Перевірка роздільності
└── Golden frames для Spike B

Фаза 2: Spike B — Vision LLM (розширений)
├── Описи (batch, 4 моделі)
├── Vision LLM як OCR (code accuracy з ground truth)
├── Combined prompt (VD + text extraction)
├── STT context test
├── Two-pass vs one-pass cost/quality
├── Crop strategy test
└── РІШЕННЯ: потрібен Stage C? → визначає Фазу 2.5

Фаза 2.5: Spike C — OCR (ТІЛЬКИ якщо Vision LLM accuracy < 90%)
├── OCR engines comparison
├── LLM correction test
└── Вибір OCR engine

Фаза 3: Spike Report + Architecture Decision
├── Зведений звіт VD-SPIKE-REPORT.md
├── Фінальний вибір: Vision LLM chain, OCR engine (якщо потрібен)
└── Уточнення плану імплементації

Фаза 4: Implementation — VD module
├── VD-001: schemas.py — Pydantic models для всіх stages
├── VD-002: pip_tracker.py — PiP detection + dynamic tracking
├── VD-003: frame_sampler.py — Stage A (scene_detect + dHash + PiP + cooldown)
├── VD-004: visual_analyzer.py — Stage B (two-pass Vision LLM, parallel batches)
├── VD-005: ocr_extractor.py — Stage C [УМОВНИЙ]
├── VD-006: aggregation.py — Stage D (merge + cross-ref + dedup)
├── VD-007: pipeline.py — VDPipeline orchestrator + temp cleanup
└── VD-008: external_services.yaml — нові actions + chains

Фаза 5: Integration
├── VD-009: Redesign VideoProcessor (STT + VD, semi-sequential)
├── VD-010: Audio chunking для OpenAI fallback (>25MB)
├── VD-011: Update factory.py — wire VD pipeline
├── VD-012: Extend ChunkType enum + Alembic migration
├── VD-013: Update MergeStep для нових ChunkTypes
└── VD-014: E2E тест на тестовому відео

Фаза 6: Polish
├── VD-015: Unit tests для кожного stage
├── VD-016: Config env vars
├── VD-017: Documentation update
├── VD-018: Temp files cleanup (frames, compressed audio)
└── VD-019: Performance profiling (frame extraction → asyncio.to_thread)
```

---

## 13. Відкриті питання

### Визначаються під час spike

| # | Питання | Spike |
|---|---|---|
| Q1 | Scene detection vs fps — що дає кращий baseline? | A |
| Q2 | Оптимальний dHash threshold і hash_size? | A |
| Q3 | PiP tracking: чи temporal diff достатній? Чи потрібен Vision LLM re-detect? | A |
| Q4 | Чи ефективний cooldown при live coding? | A |
| Q5 | Мінімальна роздільність для надійного читання коду? | A |
| Q6 | Який Vision LLM найкраще описує код/слайди? | B |
| Q7 | **Vision LLM як OCR — чи достатня accuracy для Python коду? (≥90%?)** | B |
| Q8 | Combined prompt (VD+text) vs окремі — різниця в якості? | B |
| Q9 | STT контекст в промпті — чи покращує описи? | B |
| Q10 | Two-pass vs one-pass — cost savings vs quality loss? | B |
| Q11 | Crop strategy — чи покращує code recognition? | B |
| Q12 | Який OCR engine для dark-theme IDE? **(тільки якщо Q7 < 90%)** | C |

### Архітектурні рішення (після spike)

| # | Рішення | Опції |
|---|---|---|
| D1 | Frame extraction strategy | Scene detection + min interval vs fixed fps |
| D2 | PiP tracking method | Temporal diff only vs + periodic Vision LLM |
| D3 | VD approach | One-pass vs two-pass |
| D4 | Text extraction | Vision LLM unified vs separate OCR engine |
| D5 | STT context | Підхід A (паралельно) vs підхід B (semi-sequential) |
| D6 | VD fallback chain | Gemini Flash → GPT-4o; або Gemini Flash → Pro |
| D7 | OCR engine (якщо потрібен) | Surya vs PaddleOCR vs Tesseract vs Google Vision |
| D8 | Зберігання frames | Тимчасові файли vs S3 persistent |
| D9 | Нові ChunkTypes | Alembic migration для enum |

### Ризики

| Ризик | Ймовірність | Вплив | Мітигація |
|---|---|---|---|
| Vision LLM погано читає код з dark IDE | Середня | Високий | Crop + invert colors; fallback на OCR engine |
| PiP tracking втрачає позицію | Низька | Середній | Periodic Vision LLM re-detect |
| dHash не розрізняє слайди з малим текстом | Низька | Середній | Зменшити threshold; pixel histogram |
| Gemini Vision неточні описи коду | Середня | Середній | Fallback на GPT-4o; STT context; crop |
| Довге відео (2+ год) — memory issues | Низька | Високий | Streaming frame extraction; temp cleanup |
| Audio >25MB для OpenAI fallback | Висока | Середній | Auto-chunking з merge timestamps |
| Нова залежність конфліктує | Низька | Середній | Optional dependency group `[vd]` |

---

## Додаток: Посилання на існуючий код

| Модуль | Шлях | Relevance |
|---|---|---|
| SourceDocument model | `src/course_supporter/models/source.py` | ChunkType enum extension |
| STT Provider ABC | `src/course_supporter/stt/providers/base.py` | Architecture reference |
| STT Router | `src/course_supporter/stt/router.py` | Fallback chain pattern |
| STT Factory | `src/course_supporter/stt/factory.py` | Factory pattern |
| LLM ModelRouter | `src/course_supporter/llm/router.py` | Vision calls через `contents` |
| LLM Registry | `src/course_supporter/llm/registry.py` | YAML config для actions |
| Gemini Provider | `src/course_supporter/llm/providers/gemini.py` | Multi-image vision calls |
| Current VideoProcessor | `src/course_supporter/ingestion/video.py` | Redesign target |
| Slide describer | `src/course_supporter/ingestion/describe_slides.py` | image→Vision LLM pattern |
| Heavy Steps | `src/course_supporter/ingestion/heavy_steps.py` | Protocol pattern для DI |
| Ingestion Factory | `src/course_supporter/ingestion/factory.py` | Wiring point |
| Merge Step | `src/course_supporter/ingestion/merge.py` | Aggregation pattern |
| Config | `src/course_supporter/config.py` | Settings, KeyPool |
| External services | `config/external_services.yaml` | New actions to add |
| STT Spike Report | `current-doc/stt-spike/STT-SPIKE-REPORT-UA.md` | Reference spike format |
