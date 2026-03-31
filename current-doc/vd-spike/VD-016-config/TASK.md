# VD-016: Config

**Фаза:** 6 — Polish
**Пріоритет:** Medium
**Залежності:** VD-008

## Що робимо

Додаємо VD-specific settings до конфігурації проекту. Всі "магічні числа" з VD pipeline мають бути configurable через environment variables.

## Яким чином

Оновити `src/course_supporter/config.py`:

1. **dHash параметри:**
   - `VD_DHASH_SIZE: int = 16` — розмір hash (більший = точніший, повільніший)
   - `VD_DHASH_THRESHOLD: int = 25` — поріг Hamming distance для dedup

2. **Frame sampling:**
   - `VD_COOLDOWN_SEC: float = 4.0` — мінімальний інтервал між кадрами
   - `VD_MIN_FRAME_WIDTH: int = 1280` — мінімальна ширина кадру для якісного OCR
   - `VD_MIN_INTERVAL_SEC: float = 30.0` — fallback interval якщо scene detect мовчить

3. **Vision LLM:**
   - `VD_VISION_LLM_CONCURRENCY: int = 5` — max паралельних Vision LLM запитів
   - `VD_PASS1_BATCH_SIZE: int = 25` — кількість кадрів в batch для Pass 1
   - `VD_IMPORTANCE_THRESHOLD: int = 3` — мінімальний importance для Pass 2

4. **Aggregation:**
   - `VD_CROSS_REF_WINDOW_SEC: float = 5.0` — вікно cross-reference (±N секунд)

5. **Environment variable overrides:**
   - Всі параметри перевизначаються через env vars з префіксом `VD_`
   - Pydantic Settings автоматично зчитує env vars

## Результат

- Оновлений `src/course_supporter/config.py` з VD settings
- Всі VD параметри configurable через environment variables
- Sensible defaults для всіх параметрів
- Документація defaults в docstrings

## Як перевіряємо

```bash
uv run mypy src/course_supporter/config.py             # strict, no errors
uv run ruff check src/course_supporter/config.py        # no lint errors
# Test override:
VD_DHASH_THRESHOLD=30 uv run python -c "
from course_supporter.config import get_settings
s = get_settings()
assert s.vd_dhash_threshold == 30
print('Override works')
"
```
