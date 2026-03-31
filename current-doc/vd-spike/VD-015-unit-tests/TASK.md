# VD-015: Unit Tests

**Фаза:** 6 — Polish
**Пріоритет:** High
**Залежності:** VD-001 — VD-007

## Що робимо

Пишемо unit tests для кожного модуля VD pipeline. Mock LLM responses, тестуємо логіку без зовнішніх сервісів.

## Яким чином

Створити тести в `tests/unit/`:

1. **`test_vd_frame_sampler.py`:**
   - dHash computation: однакові зображення → малий Hamming distance
   - dHash dedup: дублікати видаляються, унікальні залишаються
   - Cooldown logic: при швидких scene changes — не більше 1 frame per cooldown_sec
   - Scene detection integration: mock FFmpeg output → правильний parsing
   - PiP masking: зображення з різним PiP але однаковим контентом → однаковий hash

2. **`test_vd_pip_tracker.py`:**
   - Vision LLM detection: mock response → правильний PiPMask
   - Temporal diff: mock cv2.absdiff → правильне виявлення руху
   - No-PiP handling: Vision LLM каже "no PiP" → порожній PiPMask, skip рівні 2-3
   - PiPEvent logging: правильні timestamps та methods

3. **`test_vd_visual_analyzer.py`:**
   - Pass 1 filtering: mock LLM response → кадри з importance < 3 відфільтровані
   - Pass 2 batching: правильна кількість batches, parallel execution
   - Smart crop: правильне обрізання для різних scene_types
   - STT context injection: контекст додається до prompt коли доступний

4. **`test_vd_aggregation.py`:**
   - Merge: STT + VD chunks об'єднуються в правильному порядку
   - Cross-reference: chunks з ±5 sec window зв'язуються
   - Deduplication: однаковий текст з різних джерел → один chunk
   - Priority ordering: VISUAL_DESCRIPTION > CODE_BLOCK > TRANSCRIPT

5. **Загальне:**
   - Використовувати `pytest` fixtures для mock data
   - Mock LLM responses через `unittest.mock` або `pytest-mock`
   - Async tests через `pytest-asyncio`

## Результат

- `tests/unit/test_vd_frame_sampler.py`
- `tests/unit/test_vd_pip_tracker.py`
- `tests/unit/test_vd_visual_analyzer.py`
- `tests/unit/test_vd_aggregation.py`
- Всі тести зелені, coverage > 80% для VD модулів

## Як перевіряємо

```bash
uv run pytest tests/unit/test_vd_*.py -v              # all VD unit tests
uv run pytest tests/unit/test_vd_*.py --cov=course_supporter.vd --cov-report=term-missing
```
