# VD Spike Report — Фінальний звіт

**Дата:** 2026-04-02
**Автор:** AI-assisted analysis (Claude Opus 4.6 + manual verification)
**Статус:** Final

---

## 1. Мета spike-досліджень

Визначити оптимальну архітектуру video processing pipeline для витягування візуального контенту з навчальних відео (Python курси, українською). Три основні питання:

1. **Spike A:** Як ефективно витягувати унікальні кадри з відео?
2. **Spike B:** Яка модель і промпт найкраще описують візуальний контент (код, слайди, термінал)?
3. **Spike C:** Чи потрібен окремий OCR engine? (умовний, якщо Vision LLM < 90%)

---

## 2. Spike A: Frame Sampling + PiP Tracking — DONE

**Скрипт:** `scripts/spike_frame_sampling.py`
**Результати:** `current-doc/vd-spike/VD-SPIKE-A/RESULTS.md`
**Тестові відео:** 2 (10 хв слайди, 27 хв з кодом)

### Ключові знахідки

| Аспект | Результат | Рішення |
|--------|-----------|---------|
| FFmpeg scene detect | 6-37 кадрів (мало hard cuts у лекціях) | Не як основна стратегія |
| Fixed FPS | fps=0.5 → 318 кадрів → dHash → 11-16 унікальних | **Основна стратегія** |
| dHash hash_size | 8 занадто грубий, 12 добрий, 16 найточніший | **hash_size=16** |
| dHash threshold | 5% (dist>12) — найбільше покриття | **5% для production** |
| PiP detection | Temporal diff: confidence 0.73, zone motion 12.15 (4x від наступної) | **Temporal diff достатній** |
| PiP mask | hash_size=12: 24→13 кадрів (−45% false positives) | **Обов'язковий** |
| Cooldown | Без ефекту на слайд-відео; потрібен для live coding | **4 сек, 3 consecutive** |
| Min resolution | 1280x720 достатньо для читання коду | **720p мінімум** |

### Обрані параметри

```
fps=0.5, hash_size=16, threshold=5%, PiP mask=on, cooldown=4s/3
```

Golden frames: 17 (sample 1) + 91 (sample 2) = 108 кадрів для Spike B.

---

## 3. Spike B: Vision LLM — DONE

**Скрипти:** `scripts/spike_vision_llm.py` (v1), `scripts/spike_vd_multimodel.py` (v3), `scripts/spike_vd_pipeline.py`
**Ground truth:** `current-doc/vd-spike/VD-SPIKE-B/ground-truth/` (10 файлів)
**State dir:** `current-doc/vd-spike/VD-SPIKE-B/pipeline/`

### 3.1. Еволюція підходів

| Версія | Підхід | Accuracy | Проблеми |
|--------|--------|----------|----------|
| v1 | Single frame, JSON output, combined prompt (VD+OCR) | 65% (OCR), 93% (GPT-4o) | JSON conflicts з code blocks |
| v2 | Sliding window (3-5 frames) | 42-55% | **Гірше** за single-frame |
| **v3** | **Dense Coverage + Hierarchical Memory, Markdown output** | **99.3% (lite)** | Обраний підхід |

### 3.2. Spike B v3: Dense Coverage + Hierarchical Memory

#### Архітектура

```
Golden frames + Gap fill (no gap > 15s)
    ↓
Scene segmentation (dHash > 20% OR time_gap > 10s)
    ↓
Eyes (per-frame Vision LLM, 1-3 images)
    ↓
Instant Memory (code-based merge within scene)
    ↓
Scene Memory (per-scene synthesis)
    ↓
Course Memory (running context → feeds back to Eyes)
```

#### Key Design Decisions

**Dense Coverage (не Two-pass classification):**
- Spike показав що classification → filter → detail гірше ніж обробити все
- gap fill забезпечує no gap > 15s між кадрами
- Scene boundaries: dHash > 20% OR time_gap > 10s → 98 scenes з 151 frames

**Eyes: 1-3 images per call:**
- MAIN frame (обов'язковий) + до 2 previous frames (within 7s, same scene)
- Previous images допомагають з occlusion recovery (popup перекрив код → видно на попередньому кадрі)
- Prompt фокусує на опис MAIN frame

**Markdown output (не JSON):**
- JSON conflicts з code blocks всередині string values
- Markdown дозволяє вкладені code blocks без escaping
- Формат: Scene Composition → Elements → detailed per-element descriptions

**Hierarchical Memory (text-only, той самий model):**
- Instant: merge кадрів всередині сцени — спочатку code-based (overlap detection), fallback на LLM
- Scene: synthesis на рівні сцени (type, summary, topics, importance)
- Course: running context (≤200 слів), передається назад в Eyes для наступної сцени

#### Prompt v3

Language-agnostic Markdown. Кожен візуальний елемент описується окремо з type-specific fields:
- `code_area`: code_type (source/REPL/output), language, exact_code
- `text_area`: content_type (heading/body/label), language, text
- `ui_element`: application name, key info
- `image_or_diagram`: what is depicted

### 3.3. Порівняння моделей

**Тест:** 60 frames (22 scenes з GT-кадрами + сусідні), 10 GT frames.

| Model | RPM | Eyes done | Eyes avg | Merged avg | Формат |
|-------|-----|-----------|----------|------------|--------|
| **gemini-3.1-flash-lite-preview** | 15 | **60/60** | **99.3%** | **99.3%** | Code в code blocks |
| gemini-2.5-flash | 5 | 20/60 | 77.4%* | 61.7%* | Slide code як text |

*\* 2.5-flash на 4 GT фреймах; golden_006 (17.1%) через format issue*

**Per-frame accuracy (lite, виправлений GT):**

| Frame | Eyes | Merged | Примітка |
|-------|------|--------|----------|
| golden_006 | 100% | 100% | |
| golden_012 | 100% | 100% | |
| golden_017 | 92.6% | 92.6% | Модель поміняла `1E6`/`1e6` місцями |
| golden_027 | 100% | 100% | |
| golden_042 | 100% | 100% | |
| golden_060 | 100% | 100% | |
| golden_065 | 100% | 100% | |
| golden_075 | 100% | 100% | Merge recovered occluded code |
| golden_080 | 100% | 100% | |
| golden_085 | 100% | 100% | |

### 3.4. GT Corrections (session 17)

5 з 10 GT файлів мали помилки. Без виправлення accuracy штучно занижувалась:

| Frame | Помилка в GT | Вплив на lite accuracy |
|-------|-------------|----------------------|
| golden_006 | Неповний (тільки console), `type(1111.6)` → `type(11111.0)` | 66.7% → 100% |
| golden_012 | `fa12` → `faa12`, `64018` → `1026578` | 94.8% → 100% |
| golden_017 | Хибні REPL outputs (`1_000_000` замість `1000000`), неправильний порядок блоків | 54.1% → 92.6% |
| golden_042 | `False + True` → `False + True * 4`, `1` → `4` | 35.5% → 100% |
| golden_075 | Включав код прихований за autocomplete popup | 23.6% → 100% |

### 3.5. Проблеми 2.5-flash

1. **Format issue:** Slide code описаний як text, не в fenced code blocks → `_extract_code_blocks()` не витягує
2. **Merge corruption:** Instant memory змінив `0.1 + 0.2` → `0.1 * 0.2` і `0.937` → `0.837` (golden_027)
3. **Slower:** 5 RPM vs 15 RPM для lite

### 3.6. Metric Limitations

**Greedy sequential char matching** — порядок блоків впливає на accuracy. Якщо GT має блоки [A,B,C] а модель [B,A,C], matcher не знайде B після A. Виправлено: GT block order вирівняний з top-to-bottom visual order.

---

## 4. Spike C: OCR — SKIPPED

**Рішення:** Не потрібен. lite дає 99.3% accuracy для Python коду, слайдів, terminal output.

---

## 5. Відповіді на відкриті питання

| # | Питання | Відповідь |
|---|---------|-----------|
| Q1 | Scene detect vs fps? | fps=0.5 основна стратегія; scene detect як доповнення |
| Q2 | Оптимальний dHash? | hash_size=16, threshold=5% |
| Q3 | PiP tracking? | Temporal diff достатній (confidence 0.73) |
| Q4 | Cooldown ефективний? | Не впливає на слайд-відео; потрібен для live coding |
| Q5 | Мінімальна роздільність? | 720p достатньо |
| Q6 | Найкращий Vision LLM? | **gemini-3.1-flash-lite-preview** |
| Q7 | Vision LLM accuracy ≥90%? | **Так, 99.3%** → Stage C не потрібен |
| Q8 | Combined vs separate prompt? | Combined (Markdown v3) — найкращий |
| Q9 | STT context покращує? | Не тестовано в v3; рекомендація — semi-sequential |
| Q10 | Two-pass vs one-pass? | **Single-pass + hierarchical memory** |
| Q11 | Crop strategy? | Не тестовано; не потрібно при 99.3% |
| Q12 | OCR engine? | Не потрібен |

## 6. Архітектурні рішення

| # | Рішення | Вибір | Обґрунтування |
|---|---------|-------|---------------|
| D1 | Frame extraction | fps=0.5 + dHash 5% + gap fill ≤15s | Spike A |
| D2 | PiP tracking | Temporal diff only | Confidence 0.73, Vision LLM не потрібен |
| D3 | VD approach | **Single-pass + hierarchical memory** | Краще за two-pass classify→filter→detail |
| D4 | Text extraction | Vision LLM unified (Markdown output) | 99.3% accuracy |
| D5 | STT context | TBD: semi-sequential рекомендовано | Не тестовано, але STT готовий раніше |
| D6 | VD model | gemini-3.1-flash-lite-preview (єдиний) | 15 RPM, 99.3%, найдешевший |
| D7 | OCR engine | Не потрібен | Accuracy >90% |
| D8 | Зберігання frames | TBD (temp files vs S3) | Визначити при implementation |
| D9 | Нові ChunkTypes | TBD | Визначити при implementation |

## 7. Ризики та відкриті питання для implementation

### Нетестовані аспекти

1. **STT + VD cross-modal alignment** — як верифікувати що STT і VD описують те саме? Temporal offset, semantic mismatch, competing truth (лектор каже одне, на екрані інше)
2. **Live coding відео** — cooldown тестувався тільки на слайд-відео. Потрібен тест на відео з набором коду в реальному часі
3. **Довгі відео (>1 год)** — course memory scalability, quota management
4. **Paid API** — spike використав free tier (20 RPD). Production потребує paid Gemini API
5. **Crop strategy** — не тестувалось; може покращити accuracy для дрібного тексту

### Metric improvement

Поточний greedy sequential matcher — порядко-залежний і known to undercount. Для production рекомендується:
- Per-block matching (кожен GT block порівнюється з найкращим model block)
- Або token-level F1 score
