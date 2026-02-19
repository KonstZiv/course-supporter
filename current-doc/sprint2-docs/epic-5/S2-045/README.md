# S2-045: SlideVideoMapping migration

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 2h

---

## Мета

Існуючі маппінги мігровані на нову структуру

## Що робимо

Alembic migration: старі маппінги → нова таблиця

## Як робимо

1. Створити нову таблицю (якщо не через ALTER)
2. Мігрувати дані: визначити presentation/video entry по context
3. validation_state = validated для існуючих (вже працюють)
4. Downgrade migration

## Очікуваний результат

Існуючі маппінги працюють в новій структурі

## Як тестуємо

**Автоматизовано:** Migration test: upgrade → data intact → downgrade → data intact

**Human control:** Перевірити на staging що існуючі маппінги мігрували правильно

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
