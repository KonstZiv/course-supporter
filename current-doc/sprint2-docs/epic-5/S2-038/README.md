# S2-038: SlideVideoMapping ORM redesign

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 3h

---

## Мета

Нова модель з FK на MaterialEntry і validation state

## Що робимо

Оновити SlideVideoMapping: додати presentation_entry_id, video_entry_id, validation fields

## Як робимо

1. Нові поля: presentation_entry_id (FK), video_entry_id (FK), validation_state, blocking_factors (JSONB), validation_errors (JSONB), validated_at
2. MappingValidationState enum
3. Alembic migration

## Очікуваний результат

Маппінг зв'язує конкретну презентацію з конкретним відео з validation tracking

## Як тестуємо

**Автоматизовано:** Unit test: create mapping з FK, validation state transitions

**Human control:** Перевірити в DB структуру таблиці

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
