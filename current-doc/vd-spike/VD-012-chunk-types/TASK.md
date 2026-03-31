# VD-012: ChunkType Extension + Migration

**Фаза:** 5 — Integration
**Пріоритет:** High
**Залежності:** VD-001

## Що робимо

Розширюємо `ChunkType` enum новими типами для VD pipeline. Якщо enum зберігається як DB enum — створюємо Alembic migration.

## Яким чином

1. **Extend ChunkType enum:**
   Оновити `src/course_supporter/models/source.py`:
   ```python
   class ChunkType(str, Enum):
       # Existing:
       TRANSCRIPT = "transcript"
       # ... інші існуючі

       # New VD types:
       VISUAL_DESCRIPTION = "visual_description"
       CODE_BLOCK = "code_block"
       SLIDE_OCR = "slide_ocr"
       TERMINAL_OUTPUT = "terminal_output"
       DIAGRAM_DESCRIPTION = "diagram_description"
   ```

2. **Alembic migration:**
   - Перевірити чи `ChunkType` зберігається як PostgreSQL ENUM type
   - Якщо так — створити migration: `ALTER TYPE chunktype ADD VALUE 'visual_description'` тощо
   - Alembic migration має бути idempotent (IF NOT EXISTS де можливо)

3. **Backwards compatibility:**
   - Перевірити що існуючий код не ламається з новими enum values
   - Existing TRANSCRIPT chunks мають працювати як раніше
   - Нові chunk types не мають впливати на існуючі queries

## Результат

- Оновлений `ChunkType` enum з 5 новими значеннями
- Alembic migration (якщо потрібна)
- Backwards compatibility збережена

## Як перевіряємо

```bash
uv run mypy src/course_supporter/models/source.py      # strict, no errors
uv run ruff check src/course_supporter/models/          # no lint errors
uv run python -c "
from course_supporter.models.source import ChunkType
print(ChunkType.VISUAL_DESCRIPTION)
print(ChunkType.CODE_BLOCK)
"
# Якщо є migration:
make db-upgrade                                         # migration applies cleanly
make db-downgrade                                       # rollback works
make db-upgrade                                         # re-apply works
```
