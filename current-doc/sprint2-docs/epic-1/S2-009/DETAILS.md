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

1. on_ingestion_complete(material_entry_id, success, result):
   - Оновити MaterialEntry (processed_content або error_message)
   - Очистити pending_job_id/pending_since
   - Інвалідувати content_fingerprint → cascade _invalidate_up
   - Знайти SlideVideoMappings де цей матеріал є blocking factor → revalidate
2. Інтегрувати callback в ARQ task wrapper

---

## Очікуваний результат

Завершення ingestion каскадно оновлює entry, fingerprints і pending маппінги

---

## Тестування

### Автоматизовані тести

Integration test: ingestion complete → entry updated + fingerprints invalidated + mappings revalidated

### Ручний контроль (Human testing)

Завантажити матеріал з маппінгом в pending_validation → дочекатись ingestion → перевірити що маппінг став validated

---

## Checklist перед PR

- [ ] Реалізація відповідає архітектурним рішенням Sprint 2 (AR-*)
- [ ] Код проходить `make check` (ruff + mypy + pytest)
- [ ] Unit tests написані і покривають основні сценарії
- [ ] Edge cases покриті (error handling, boundary values)
- [ ] Error messages зрозумілі і містять hints для користувача
- [ ] Human control points пройдені
- [ ] Документація оновлена якщо потрібно (ERD, API docs, sprint progress)
- [ ] Перевірено чи зміни впливають на наступні задачі — якщо так, оновити їх docs

---

## Нотатки

_Простір для нотаток виконавця під час роботи над задачею._
