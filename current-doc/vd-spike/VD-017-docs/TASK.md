# VD-017: Documentation

**Фаза:** 6 — Polish
**Пріоритет:** Low
**Залежності:** VD-014

## Що робимо

Оновлюємо проектну документацію з інформацією про VD pipeline. Додаємо опис нових модулів до Architecture section.

## Яким чином

1. **Оновити CLAUDE.md:**
   - Додати `vd/` до Architecture section:
     - `src/course_supporter/vd/` — Visual Description pipeline
     - Перерахувати модулі: schemas, pip_tracker, frame_sampler, visual_analyzer, ocr_extractor, aggregation, pipeline
   - Оновити Pipeline Flow diagram з VD stage
   - Додати нові команди якщо є (e.g., `uv sync --extra vd`)

2. **Оновити API docs (якщо VD впливає на endpoints):**
   - Якщо SourceDocument response schema змінилась — оновити endpoint docs
   - Нові chunk types мають бути задокументовані

3. **Опис нових модулів:**
   - Коротко (1-2 рядки) для кожного нового модуля
   - Зв'язок з existing modules (ingestion, llm, stt)

## Результат

- Оновлений `CLAUDE.md` з VD pipeline info
- API docs оновлені (якщо потрібно)
- Architecture section відображає реальний стан коду

## Як перевіряємо

```bash
# Manual review: прочитати оновлений CLAUDE.md
# Перевірити що Architecture section відповідає фактичній структурі коду
ls src/course_supporter/vd/                            # порівняти з docs
```
