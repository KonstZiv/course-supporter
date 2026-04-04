# VD-011: E2E Integration Test

**Фаза:** 5 — Integration
**Пріоритет:** Високий
**Залежності:** VD-001..010 (всі завершені)
**Блокує:** Фаза 6

---

## Мета

Єдиний E2E скрипт `scripts/cp4_e2e_test.py`, який за один прогін виконує повний pipeline відеообробки та генерує HTML-звіт з трьома секціями: **CP-4** (VD Pipeline), **VD-011** (E2E integration), **CP-5** (Cross-modal alignment).

Скрипт має підтвердити, що всі компоненти (FrameSampler → VisualAnalyzer → MemoryPipeline → STT Router → CrossModalAligner) працюють разом як єдина система без збоїв.

---

## Контекст та залежності

### Готові компоненти

| Компонент | Файл | Що робить |
|-----------|------|-----------|
| **FrameSampler** | `src/course_supporter/vd/frame_sampler.py` | FFmpeg → дедуплікація (5 метрик, tiered voting) → scene segmentation (3 gates) → gap fill |
| **VisualAnalyzer** | `src/course_supporter/vd/visual_analyzer.py` | Per-frame Vision LLM (Eyes), CONDITIONAL delta strategy, retry + fallback |
| **MemoryPipeline** | `src/course_supporter/vd/memory_pipeline.py` | Streaming 3-level memory: instant → scene → video |
| **VDPipeline** | `src/course_supporter/vd/pipeline.py` | Оркестратор Stage A→B→C, повертає `VDResult` |
| **STT Router** | `src/course_supporter/stt/router.py` | Fallback chain STT провайдерів → `STTResult` |
| **CrossModalAligner** | `src/course_supporter/ingestion/alignment.py` | 4-phase alignment: temporal → semantic → conflicts → verification |
| **VideoProcessor** | `src/course_supporter/ingestion/video.py` | Паралельний STT ‖ VD, graceful degradation |
| **Factory** | `src/course_supporter/ingestion/factory.py` | `create_vd_pipeline()`, `create_processors()` |
| **KeyPool** | `src/course_supporter/key_pool.py` | Round-robin Gemini API key rotation |

### Моделі та rate limits

- **Primary:** `gemini-2.5-flash` (5 RPM/key)
- **Fallback:** `gemini-3.1-flash-lite-preview` (15 RPM/key)
- **10 ключів** Gemini (free tier)
- Retry на 429 з exponential backoff (base 40s)

### Попередні checkpoint-скрипти (reference)

- `cp1_frame_sampler_test.py` — Stage A ізольовано, HTML gallery
- `cp2_*.py` (5 скриптів) — Eyes якість, delta A/B, model comparison
- `cp3_memory_test.py` — streaming memory hierarchy
- `cp4_gate_review.py` — VDPipeline E2E з checkpoint/resume

---

## Що робимо

### Крок 0: VideoProcessor — Whisper → STT Router

Перед E2E тестом потрібно замінити `stt: WhisperVideoProcessor` на `stt_router: STTRouter` у `VideoProcessor`:

**Файли що змінюються:**
- `src/course_supporter/ingestion/video.py` — VideoProcessor приймає `stt_router: STTRouter` замість `WhisperVideoProcessor`
- `src/course_supporter/ingestion/factory.py` — `create_processors()` передає `stt_router`
- `tests/unit/test_ingestion/test_video.py` — оновити мокі
- `tests/unit/test_ingestion/test_video_whisper.py` — оновити `TestVideoProcessorParallel`

**Логіка заміни:**
```
VideoProcessor.process():
  1. FFmpeg extract audio → temp .wav file
  2. stt_router.transcribe(action="transcribe", audio_path=wav_path) → STTResult
  3. STTResult.segments → ContentChunk(TRANSCRIPT, start_sec, end_sec)
  4. VDPipeline.process(video_path) → VDResult (паралельно з п.2)
  5. VDResult.scenes → ContentChunk(VISUAL_SCENE, ...)
  6. Повернути SourceDocument з обома типами chunks
```

`WhisperVideoProcessor` залишається в коді (не видаляємо), але `VideoProcessor` його більше не використовує.

---

### Крок 1: E2E скрипт `scripts/cp4_e2e_test.py`

Один скрипт, один набір API calls, один HTML звіт з трьома секціями.

#### CLI інтерфейс

```bash
uv run python scripts/cp4_e2e_test.py [video_path] [--max-sec N] [--stt-provider PROVIDER]
```

- `video_path` — шлях до відео (default: `tmp/cp1-test/video1_python_16min.mp4`)
- `--max-sec` — обмежити перших N секунд (default: 300 = 5 хв)
- `--stt-provider` — STT провайдер override (default: за конфігом router)

#### Pipeline виконання

```
Video file (local .mp4)
│
├─── [Паралельно A] STT Branch:
│    ├── FFmpeg extract audio → temp .wav
│    ├── STT Router → STTResult
│    │   └── segments: list[STTSegment(start_sec, end_sec, text)]
│    └── → list[ContentChunk(TRANSCRIPT)]
│
├─── [Паралельно B] VD Branch:
│    ├── Stage A: FrameSampler.sample(video_path)
│    │   └── → FrameSamplingResult(frames, scenes, pip_mask)
│    ├── Stage B: VisualAnalyzer.analyze_scene() × N scenes
│    │   └── → list[EyesResult] + SceneMemory per scene
│    ├── Stage C: MemoryPipeline — streaming updates
│    │   └── → VideoMemory (running video context)
│    └── → VDResult(scenes, video_memory, frames_total, frames_analyzed, model)
│
├─── Conversion to ContentChunks:
│    ├── STT: STTSegment → ContentChunk(TRANSCRIPT, text, start_sec, end_sec)
│    └── VD: SceneAnalysis → ContentChunk(VISUAL_SCENE, scene_memory.summary,
│            start_sec, end_sec, metadata={scene_id, scene_type, importance, topics})
│
├─── CrossModalAligner.align(stt_chunks, vd_chunks)
│    └── → AlignmentReport(segments, coverage_gaps, orphans, conflicts, semantic_coverage)
│
└─── SourceDocument(
         source_type=VIDEO,
         chunks=[TRANSCRIPT... + VISUAL_SCENE...],
         metadata={strategy, stt_segments, vd_scenes}
     )
```

#### Checkpoint/Resume

За прикладом `cp4_gate_review.py`:
- JSON checkpoint після кожної scene (`tmp/cp4-e2e/checkpoint_{video}.json`)
- При рестарті — пропускаємо вже оброблені scenes
- Фінальний результат: `tmp/cp4-e2e/result_{video}.json`

---

### Крок 2: HTML звіт — три секції

#### Секція 1: CP-4 — VD Pipeline Quality

Що показуємо:
- **Stats dashboard:** кількість scenes / frames / tokens / timing (Stage A vs B vs C)
- **Timeline:** scenes з type / importance / summary
- **Scene details:** frame grid з thumbnails + Eyes responses
- **Video memory:** фінальний running context
- **Delta efficiency:** % delta vs full, token savings

Що перевіряємо (acceptance criteria):
- [ ] Pipeline працює без crash
- [ ] Кожна scene має ≥1 frame з EyesResult
- [ ] Video memory — зв'язна розповідь, не набір фрагментів
- [ ] Немає hallucinations (код з відео ≠ вигаданий код)
- [ ] Delta strategy працює: LOW/MEDIUM frames отримують DELTA

#### Секція 2: VD-011 — E2E Integration

Що показуємо:
- **SourceDocument summary:** загальна кількість chunks за типом (TRANSCRIPT / VISUAL_SCENE)
- **Coverage heatmap:** timeline з позначками STT / VD покриття
- **Chunk balance:** ratio TRANSCRIPT:VISUAL_SCENE
- **Metadata integrity:** всі chunks мають start_sec/end_sec, metadata правильна
- **Graceful degradation test:** (окремий прогін?) — що відбувається якщо VD fails

Що перевіряємо (acceptance criteria):
- [ ] SourceDocument містить chunks обох типів (TRANSCRIPT + VISUAL_SCENE)
- [ ] Всі chunks мають валідні start_sec / end_sec (не None для відео)
- [ ] Coverage > 90% тривалості відео (хоча б одним типом chunk)
- [ ] Metadata: `strategy == "stt+vd"`, `stt_segments > 0`, `vd_scenes > 0`
- [ ] Performance: 5-хв відео < 15 хвилин обробки

#### Секція 3: CP-5 — Cross-Modal Alignment

Що показуємо:
- **AlignmentReport summary:** кількість aligned segments, coverage gaps, orphans, conflicts
- **Alignment timeline:** visualізація aligned segments з confidence scores
- **Orphan analysis:** STT-only та VD-only сегменти з поясненням (silent visual / verbal-only)
- **Conflict list:** STT↔VD суперечності з контекстом
- **Semantic overlap:** per-segment Jaccard similarity

Що перевіряємо (acceptance criteria):
- [ ] `semantic_coverage > 0.5` (≥50% segments мають STT+VD overlap)
- [ ] Кількість `coverage_gaps` з `gap_type == "neither"` = 0 (немає "мертвих зон")
- [ ] `conflicts` — перелік для ручного review, не critical blockers
- [ ] Orphans — допустимі (silent visual, verbal-only), але не >30% від загальної кількості

---

## Data Flow — ключові типи

```
STTRouter.transcribe() → STTResult
    .segments: list[STTSegment(start_sec, end_sec, text)]
    .provider: str
    .model_id: str
    .latency_ms: int
    .audio_duration_sec: float

VDPipeline.process() → VDResult
    .scenes: list[SceneAnalysis]
        .scene: Scene(scene_id, frame_ids, start_sec, end_sec)
        .eyes_results: list[EyesResult(frame_id, timestamp_sec, response, is_delta, importance)]
        .scene_memory: SceneMemory(summary, scene_type, topics, importance)
    .video_memory: VideoMemory(text, scenes_processed)
    .frames_total: int
    .frames_analyzed: int
    .model: str

CrossModalAligner.align() → AlignmentReport
    .segments: list[AlignedSegment(start_sec, end_sec, stt_text, vd_scene_id, vd_summary,
                                    semantic_overlap, conflicts, alignment_confidence)]
    .coverage_gaps: list[CoverageGap(start_sec, end_sec, gap_type)]
    .vd_orphans: list[int]      # scene_ids without STT
    .stt_orphans: list[int]     # STT indices without VD
    .conflicts: list[str]
    .semantic_coverage: float    # 0-1

VideoProcessor.process() → SourceDocument
    .source_type: SourceType.VIDEO
    .chunks: list[ContentChunk(chunk_type, text, start_sec, end_sec, metadata)]
    .metadata: {strategy: "stt+vd"|"stt", stt_segments: int, vd_scenes: int}
```

---

## Обмеження та правила виконання

1. **Один foreground процес** — ніколи не запускати паралельно
2. **Gemini free tier** — 10 ключів × 5 RPM = 50 RPM max для Eyes; quota оновлюється щодня
3. **Checkpoint/resume** — обов'язково, щоб при crash не втрачати вже оброблені scenes
4. **Одна модель для всіх кроків тесту** — не міксувати gemini-2.5-flash та gemini-3.1-flash-lite-preview в межах одного прогону (окрім fallback на 429)
5. **STT провайдер** — використовувати той що налаштований в STT Router (ElevenLabs / Deepgram / OpenAI)
6. **Тестове відео** — `video1_python_16min.mp4` з tmp/cp1-test/, перших 300 секунд

---

## Структура файлів

```
scripts/
  cp4_e2e_test.py          # Основний E2E скрипт (НОВИЙ)
  _utils.py                # Існуючі helpers (thumb_b64, find_frame, load_env)

tmp/cp4-e2e/               # Output directory
  checkpoint_{video}.json   # Per-scene checkpoints
  result_{video}.json       # Final VDResult + STTResult + AlignmentReport
  e2e_report_{video}.html   # HTML звіт (3 секції)
```

---

## Acceptance Criteria — повний чеклист

### Pipeline integrity
- [ ] Pipeline працює end-to-end без crash
- [ ] STT Router повертає STTResult з segments
- [ ] VDPipeline повертає VDResult зі scenes
- [ ] CrossModalAligner повертає AlignmentReport
- [ ] SourceDocument має chunks обох типів

### Coverage & balance
- [ ] Coverage > 90% тривалості відео (union STT ∪ VD)
- [ ] ChunkType balance: є і TRANSCRIPT і VISUAL_SCENE
- [ ] Немає coverage gaps з `gap_type == "neither"` (повна "мертва зона")
- [ ] Semantic coverage > 0.5

### Quality
- [ ] Video memory — зв'язний опис, а не фрагменти
- [ ] Немає hallucinations в Eyes responses
- [ ] Delta strategy ефективна (>50% non-first frames → DELTA)
- [ ] Alignment conflicts — review list, не blockers

### Performance
- [ ] 5-хв відео (300s) < 15 хвилин загального часу
- [ ] Stage A (frame sampling) < 30 секунд
- [ ] Stage B+C (VD analysis + memory) — основний час, залежить від RPM

### Graceful degradation
- [ ] Якщо VD fails → SourceDocument містить тільки TRANSCRIPT chunks
- [ ] metadata.strategy == "stt" (не "stt+vd")
- [ ] Немає crash або unhandled exception

---

## ⚠️ Після завершення — CP-6: Фінальна перевірка SourceDocument

Людина перевіряє згенерований HTML-звіт:
1. SourceDocument як конспект — чи можна вчитися за цим матеріалом?
2. ChunkType balance — чи доповнюють TRANSCRIPT і VISUAL_SCENE одне одного?
3. Порівняння з baseline (STT-only SourceDocument) — що додала VD?
4. Alignment якість — чи правильно зіставлені STT↔VD сегменти?

**Очікуваний час:** ~1-2 години ручного review.
