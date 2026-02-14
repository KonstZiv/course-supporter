# S1-022: Save Course Structure (CourseStructureRepository)

## Мета

Реалізувати `CourseStructureRepository` для збереження `CourseStructure` (Pydantic) в DB через ORM. Replace-стратегія при повторному генеруванні.

## Що робимо

1. **`CourseStructureRepository`** у `storage/repositories.py` — `save(course_id, structure) -> Course`
2. **Replace strategy**: clear existing modules (cascade delete) → create new from Pydantic
3. **Static helpers**: `_create_module`, `_create_lesson`, `_create_concept`, `_create_exercise`
4. **Pydantic → ORM mapping**: SlideRange → JSONB dict, WebReference → JSONB list, empty lists → None
5. **~12 unit-тестів**: mock session, field mapping, cascade delete, flush

## Очікуваний результат

- `CourseStructureRepository(session).save(course_id, structure)` зберігає повну ієрархію
- Course.title/description оновлюються зі structure
- Existing modules видаляються (cascade) перед створенням нових
- `flush()` замість `commit()` — caller контролює transaction
- `make check` проходить

## Контрольні точки

- [ ] `save()` оновлює Course metadata
- [ ] `save()` clears existing modules before creating new
- [ ] Module/Lesson order field — auto-increment
- [ ] SlideRange → JSONB dict mapping
- [ ] WebReference → JSONB list of dicts mapping
- [ ] Empty lists → None для JSONB
- [ ] `flush()` called, `commit()` NOT called
- [ ] ValueError якщо course not found
- [ ] ~12 тестів зелені
- [ ] `make check` проходить

## Деталі

Повний spec: **T022-save-course.md**

## Блокується

- S1-019 (CourseStructure Pydantic models)
