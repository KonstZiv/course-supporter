# S2-009: Ingestion completion callback — Деталі для виконавця

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 3h

---

## Мета

Після завершення ingestion: оновити entry, інвалідувати fingerprints, trigger revalidation маппінгів

## Контекст

Ця задача є частиною Epic "Infrastructure — ARQ + Redis" (4-5 днів).
Загальна ціль epic: Task queue з persistence, concurrency control, work window, job tracking, estimates.

## Залежності

**Попередня задача:** [S2-008: Замінити BackgroundTasks → ARQ enqueue](../S2-008/README.md)

**Наступна задача:** [S2-010: Job status API endpoint](../S2-010/README.md)

---

## Детальний план реалізації

### Архітектурне рішення

Виділено окремий сервіс `IngestionCallback` (`src/course_supporter/ingestion_callback.py`) замість inline логіки в ARQ task. Це дозволяє:

1. Тестувати callback незалежно від ingestion pipeline
2. Інкапсулювати two-session pattern (caller не знає деталей)
3. Мати чіткі extension points для майбутніх Epic 3 та Epic 5

### Реалізація

1. **`IngestionCallback` сервіс** (`ingestion_callback.py`):
   - `on_success(job_id, material_id, content_json)`:
     - SourceMaterial → done (з content_snapshot)
     - Job → complete (з result_material_id)
     - `_invalidate_fingerprints()` — no-op stub (Epic 3, S2-027)
     - `_revalidate_blocked_mappings()` — no-op stub (Epic 5, S2-042)
     - session.commit()
   - `on_failure(job_id, material_id, error_message)`:
     - Job → failed (з error_message)
     - SourceMaterial → error (з error_message)
     - `_revalidate_blocked_mappings()` — no-op stub
     - session.commit()

2. **Рефакторинг `arq_ingest_material`** (`api/tasks.py`):
   - Тонкий оркестратор: check_work_window → activate job → process → delegate to callback
   - Success path: callback.on_success() після виходу з processing session
   - Failure path: session.rollback() → callback.on_failure() (fresh session inside callback)

3. **Extension hooks** — порожні async методи з документацією:
   - `_invalidate_fingerprints()` — план для Epic 3 (S2-027)
   - `_revalidate_blocked_mappings()` — план для Epic 5 (S2-042)

### Важливо: SourceMaterial → MaterialEntry міграція

Зараз callback працює з `SourceMaterial` (Sprint 0 модель). При імплементації S2-014 (MaterialEntry ORM) потрібно:
- Замінити `SourceMaterialRepository` → `MaterialEntryRepository`
- Оновити field names: `content_snapshot` → `processed_content`, додати clearing `pending_job_id`/`pending_since`
- **Перевірити всі тести** — вони прив'язані до конкретних полів
- Див. docstring в `ingestion_callback.py` для повного списку змін

---

## Очікуваний результат

- `IngestionCallback` сервіс з on_success/on_failure і no-op hooks
- `arq_ingest_material` рефакторено як тонкий оркестратор
- 15 unit тестів: success path, failure path, error propagation, hooks called, integration з ARQ task

---

## Тестування

### Автоматизовані тести (`tests/unit/test_ingestion_callback.py`)

- **TestOnSuccess** (5 тестів): material→done, job→complete, session committed, fingerprint hook called, revalidate hook called
- **TestOnFailure** (4 тести): job→failed, material→error, session committed, revalidate hook called
- **TestOnSuccessErrors** (2 тести): material not found propagates, job not found propagates
- **TestOnFailureErrors** (1 тест): job not found propagates
- **TestHooksAreNoOp** (2 тести): stubs callable without error
- **TestCallbackIntegrationWithArqTask** (2 тести): success delegates to callback, failure delegates to callback

### Ручний контроль (Human testing)

Завантажити матеріал через API → дочекатись ingestion → перевірити:
1. Job status = complete, result_material_id заповнений
2. SourceMaterial status = done, content_snapshot заповнений
3. При помилці: Job = failed, Material = error, error_message заповнений

---

## Checklist перед PR

- [x] Реалізація відповідає архітектурним рішенням Sprint 2 (AR-3: callback)
- [x] Код проходить `make check` (ruff + mypy + pytest)
- [x] Unit tests написані і покривають основні сценарії
- [x] Edge cases покриті (error handling, not found)
- [x] Extension hooks документовані з планом реалізації
- [ ] Human control points пройдені
- [x] Вплив на наступні задачі задокументований (S2-014, S2-027, S2-042)

---

## Нотатки

### Рішення прийняті при імплементації

1. **Порожні методи замість ABC/Protocol** — простіше, замінюються на місці при Epic 3/5
2. **Callback працює з SourceMaterial** — перехід на MaterialEntry при S2-014 (документовано в коді)
3. **Two-session pattern в callback** — on_failure відкриває нову сесію, caller відповідає тільки за rollback своєї
4. **Legacy `ingest_material` не змінена** — deprecated, не використовує Job tracking
