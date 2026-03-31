# VD-004: Visual Analyzer

**Фаза:** 4 — Implementation
**Пріоритет:** Critical
**Залежності:** VD-001, VD-003

## Що робимо

Створюємо модуль для двопрохідного аналізу кадрів через Vision LLM. Pass 1 — швидка класифікація та фільтрація. Pass 2 — детальний аналіз відібраних кадрів з витягуванням тексту/коду.

## Яким чином

Створити `src/course_supporter/vd/visual_analyzer.py`:

### Pass 1: Classification (batch)

1. **Batching:** групувати кадри по 20-30 штук
2. **Vision LLM запит** через `ModelRouter` з action `visual_classification`:
   - Модель: Gemini Flash (за замовчуванням)
   - Prompt: класифікувати кожен кадр (scene_type, short description, has_text, importance 1-5)
3. **Фільтрація:** залишити тільки кадри з `importance >= 3`
4. **Результат:** `list[FrameClassification]` для кожного кадру

### Pass 2: Detailed Analysis (selective + parallel)

1. **Smart crop by scene_type:**
   - Перед аналізом обрізати кадр відповідно до типу сцени
   - IDE/terminal: обрізати до основної робочої зони
   - Slide: обрізати зайві рамки
2. **Vision LLM запит** через `ModelRouter` з action `visual_analysis`:
   - Модель: Gemini Flash → fallback GPT-4o
   - Prompt: детальний опис + витягування тексту/коду з кадру
3. **Optional STT context (approach B):**
   - Якщо доступний STT transcript — додати відповідний фрагмент транскрипту до prompt
   - Це допомагає Vision LLM краще зрозуміти контекст (що лектор пояснює в цей момент)
4. **Parallel execution:**
   - Запускати batch запити паралельно через `asyncio.Semaphore`
   - Контролювати concurrency (VISION_LLM_CONCURRENCY = 5)
5. **Результат:** `VisualAnalysisResult` з `list[VisualAnalysis]`

## Результат

- Файл `src/course_supporter/vd/visual_analyzer.py`
- Pass 1: batch classification → фільтрація по importance
- Pass 2: selective detailed analysis з parallel execution
- Використовує `ModelRouter` з actions: `visual_classification`, `visual_analysis`
- Smart crop та optional STT context

## Як перевіряємо

```bash
uv run mypy src/course_supporter/vd/visual_analyzer.py  # strict, no errors
uv run ruff check src/course_supporter/vd/               # no lint errors
uv run pytest tests/unit/test_vd_visual_analyzer.py -v   # unit tests (після VD-015)
```
