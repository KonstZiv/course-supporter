# VD-016: STT-Guided Visual Description

**Фаза:** 6 — Quality Improvement
**Пріоритет:** Високий
**Залежності:** VD-011 (E2E test — done)
**Тип:** Гіпотеза → Spike → Інтеграція

---

## Проблема

VD-011 E2E тест показав:
- **Video Memory** зациклена на `map()`, хоча перші 5 хв — list comprehension + цикли
- **IDE неправильно ідентифіковано** — VLM каже VSCode, а у відео PyCharm
- **Scene описи generic** — "instructor in circular overlay" замість конкретики
- **VLM не знає теми** — бачить код, але не знає контексту (про що лекція)

**Кореневна причина:** VD pipeline не має доступу до того, що **каже** лектор. Голос — найякісніше джерело інформації про тему, але VD працює незалежно від STT.

## Гіпотеза

Якщо передати VLM текст того, що лектор каже **в цей момент**, якість опису суттєво зросте:
- VLM знатиме на чому фокусуватись на екрані
- Правильна ідентифікація інструментів (лектор каже "PyCharm" → VLM не плутає з VSCode)
- Опис буде привʼязаний до теми лекції, а не до випадкових візуальних елементів
- Video memory отримає правильну послідовність тем

## Архітектурний підхід

### Зараз (паралельно, незалежно)

```
Video ─┬─ STT Router (30s) ──→ TRANSCRIPT chunks
       └─ VD Pipeline (14 хв) ──→ VISUAL_SCENE chunks
                                     ↓
                            CrossModalAligner → AlignedSegments
```

### Пропозиція (послідовно, STT як контекст)

```
Video → STT Router (30s) → STTResult
            ↓
        VD Pipeline (з STT контекстом)
            ↓
        VISUAL_SCENE chunks (вища якість)
            ↓
        CrossModalAligner → AlignedSegments
```

**Ціна:** +30s до загального часу (STT більше не паралельний).
**Вигода:** суттєве покращення якості VD описів.

## Що міняється

### Крок 1: STT context у Eyes prompt (spike)

**Файл:** `src/course_supporter/vd/visual_analyzer.py`

VisualAnalyzer вже має систему context blocks (memory context — instant, scene, video). STT стає ще одним context block.

Для кожного frame з timestamp `T`, знаходимо STT сегменти що покривають `[T-5s, T+5s]` і формуємо:

```
## Audio Context
At this moment, the instructor is saying:
"{stt_text}"

Focus your description on visual elements related to what the instructor
is discussing. Use the audio context to correctly identify tools, languages,
and concepts shown on screen.
```

**Prompt injection point:** між `{context_block}` та основним описом у Eyes prompt.

### Крок 2: Передача STT у VD Pipeline

**Файли:**
- `src/course_supporter/vd/pipeline.py` — `VDPipeline.process(video_path, stt_segments=None)`
- `src/course_supporter/vd/visual_analyzer.py` — `analyze_scene(..., stt_segments=None)`
- `src/course_supporter/vd/schemas.py` — можливо новий `STTContext` type

VDPipeline передає відповідні STT сегменти в VisualAnalyzer для кожної scene.

### Крок 3: VideoProcessor — послідовний замість паралельного

**Файл:** `src/course_supporter/ingestion/video.py`

```python
async def process(self, source, *, router=None):
    video_path = await self._resolve_local_path(source.source_url)

    # Step 1: STT first
    stt_chunks, stt_result = await self._run_stt(video_path)

    # Step 2: VD with STT context
    vd_chunks = await self._run_vd(video_path, stt_segments=stt_result.segments)

    return SourceDocument(chunks=stt_chunks + vd_chunks, ...)
```

### Крок 4: E2E verification (CP-7)

Один прогін `scripts/cp4_e2e_test.py` з STT-guided VD на тому ж відео.
Порівняння A/B: viewer для старих описів (VD-011) vs нових (VD-016).

## Spike план

Перед повною інтеграцією — мінімальний spike:

1. **Взяти існуючий STT кеш** (`tmp/cp4-e2e/stt_video1_python_16min_300s.json`)
2. **Один frame + STT context** — вручну додати STT текст у Eyes prompt для 1-2 frames
3. **Порівняти** відповідь VLM з/без STT context
4. **Якщо гіпотеза підтверджена** → повна інтеграція (кроки 1-4)

Це коштує 2-4 Gemini API calls.

## Acceptance Criteria

### Spike (CP-7a)
- [ ] A/B порівняння: той самий frame з STT context vs без
- [ ] VLM правильно ідентифікує IDE (PyCharm) коли STT каже "PyCharm"
- [ ] Опис фокусується на темі лекції, а не на generic візуальних елементах
- [ ] Рішення: гіпотеза підтверджена / спростована

### Повна інтеграція (якщо spike PASS)
- [ ] VisualAnalyzer приймає STT context
- [ ] VDPipeline передає STT сегменти в analyzer
- [ ] VideoProcessor — послідовний (STT → VD)
- [ ] E2E тест (CP-7b): порівняння з VD-011 baseline
- [ ] Всі існуючі тести зелені
- [ ] Video Memory описує правильну послідовність тем

## Data Flow — з STT Context

```
STTResult.segments: list[STTSegment(start_sec, end_sec, text)]
                          ↓
           (filter by frame.timestamp_sec ± window)
                          ↓
              STT text для конкретного frame
                          ↓
         Eyes prompt: {context_block} + {stt_context} + main
                          ↓
              VLM response (з урахуванням аудіо)
```

## Ризики та обмеження

1. **Prompt довшає** — STT text додає ~100-200 токенів на frame. При 54 frames = ~10K extra tokens. Прийнятно.
2. **STT українською, VD англійською** — VLM Gemini добре працює з multilingual input. Не очікуємо проблем.
3. **STT може бути неточним** — рідко, ElevenLabs якісний. Але VLM має використовувати STT як hint, не як ground truth.
4. **Послідовне виконання** — +30s. Для 16-хв відео це 0.3% overhead.
