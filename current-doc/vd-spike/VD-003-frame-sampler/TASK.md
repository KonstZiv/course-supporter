# VD-003: Frame Sampler

**Фаза:** 4 — Implementation
**Пріоритет:** Critical
**Залежності:** VD-001, VD-002

## Що робимо

Створюємо модуль для розумного семплінгу кадрів з відео. Замість витягування всіх кадрів або кожного N-го — використовуємо FFmpeg scene detection для знаходження моментів візуальних змін, а потім dHash dedup для видалення дублікатів.

## Яким чином

Створити `src/course_supporter/vd/frame_sampler.py`:

1. **FFmpeg scene detection:**
   - Запустити FFmpeg з фільтром `select='gt(scene,THRESHOLD)'` для виявлення scene changes
   - Min interval fallback: якщо між scene changes > 30 секунд, додати проміжний кадр
   - Витягнути кадри як JPEG файли у тимчасову директорію

2. **Resolution check:**
   - Перевірити що ширина кадру >= `MIN_FRAME_WIDTH = 1280`
   - Якщо менше — залогувати warning (текст може бути нечитабельним)

3. **dHash deduplication:**
   - Обчислити dHash для кожного кадру через `imagehash`
   - Configurable параметри: `hash_size` (default 16), `threshold` (default 25 — Hamming distance)
   - Видалити дублікати: якщо Hamming distance < threshold, зберегти тільки перший кадр

4. **PiP masking before hash:**
   - Перед обчисленням dHash замаскувати PiP зону (з `PiPMask` від VD-002)
   - Це запобігає false negatives коли основний контент однаковий, а PiP рухається

5. **Cooldown logic для live coding:**
   - Live coding сесії мають малі візуальні зміни між кадрами (рядок коду додався)
   - Cooldown: не брати більше 1 кадру кожні `cooldown_sec` (default 4) секунди
   - Запобігає надмірному семплінгу при швидких scene changes

6. **Повернути `FrameSamplingResult`:**
   - `frames: list[SampledFrame]` — відсортовані за timestamp
   - `pip_events: list[PiPEvent]` — від PiP tracker
   - `total_raw: int` — скільки кадрів було до dedup
   - `resolution: tuple[int, int]` — роздільна здатність відео

## Результат

- Файл `src/course_supporter/vd/frame_sampler.py`
- Основна функція що повертає `FrameSamplingResult`
- FFmpeg scene detection + dHash dedup + PiP masking + cooldown
- Всі кадри зберігаються як JPEG у тимчасовій директорії (caller відповідає за cleanup)

## Як перевіряємо

```bash
uv run mypy src/course_supporter/vd/frame_sampler.py  # strict, no errors
uv run ruff check src/course_supporter/vd/             # no lint errors
uv run pytest tests/unit/test_vd_frame_sampler.py -v   # unit tests (після VD-015)
# Manual: запустити на sample.mp4, перевірити що кадри не дублюються
```
