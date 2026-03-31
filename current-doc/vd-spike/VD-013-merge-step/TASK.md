# VD-013: Update MergeStep

**Фаза:** 5 — Integration
**Пріоритет:** Medium
**Залежності:** VD-012

## Що робимо

Оновлюємо `MergeStep` щоб він коректно обробляв нові `ChunkType` значення з VD pipeline. Перевіряємо що downstream agents (зокрема `ArchitectAgent`) можуть працювати з новими типами chunks.

## Яким чином

1. **Update MergeStep:**
   Модифікувати `src/course_supporter/ingestion/merge.py`:
   - Перевірити логіку merge для нових chunk types
   - `VISUAL_DESCRIPTION` chunks мають зберігати cross-reference до відповідних `TRANSCRIPT` chunks
   - `CODE_BLOCK` chunks мають зберігати metadata про мову програмування (якщо визначена)
   - Ordering: при merge декількох SourceDocuments, зберегти timestamp ordering

2. **Verify downstream compatibility:**
   - `ArchitectAgent` отримує merged `CourseContext`
   - Перевірити що prompt templates працюють з новими chunk types
   - Agent має розрізняти "що лектор сказав" (TRANSCRIPT) vs "що на екрані" (VISUAL_DESCRIPTION/CODE_BLOCK)

3. **Handle edge cases:**
   - SourceDocument тільки з TRANSCRIPT (старі відео без VD) — має працювати як раніше
   - SourceDocument з VD але без TRANSCRIPT (малоймовірно, але можливо)
   - Mixed: presentation + video chunks в одному CourseContext

## Результат

- Оновлений `src/course_supporter/ingestion/merge.py`
- MergeStep коректно обробляє всі нові ChunkType
- Downstream agents отримують правильну структуру
- Backwards compatibility збережена

## Як перевіряємо

```bash
uv run mypy src/course_supporter/ingestion/merge.py    # strict, no errors
uv run ruff check src/course_supporter/ingestion/       # no lint errors
uv run pytest tests/unit/test_merge.py -v               # existing tests pass
# Manual: створити SourceDocument з VD chunks, пропустити через MergeStep
```
