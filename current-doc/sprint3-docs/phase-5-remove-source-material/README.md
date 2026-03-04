# Phase 5: Remove SourceMaterial

**Складність:** M (Medium)
**Залежності:** Phase 4 (Course removed, course_id FK resolved)
**Задачі:** S3-014
**PR:** 1 PR

## Мета

Видалити legacy SourceMaterial table та весь пов'язаний dual-model branching code. Після цього ingestion працює тільки через MaterialEntry.

## Контекст

SourceMaterial — legacy таблиця з Sprint 0, прив'язана до Course (не до Node). В Sprint 2 (S2-060) додали dual-model detection в `arq_ingest_material` — MaterialEntry first, SourceMaterial fallback. Після видалення Course можна прибрати fallback.

## Критерії завершення

- [ ] SourceMaterial ORM видалений
- [ ] SourceMaterialRepository видалений
- [ ] `is_new_model` branching видалений з tasks/callback
- [ ] Legacy upload endpoint видалений
- [ ] Ingestion працює тільки через MaterialEntry
