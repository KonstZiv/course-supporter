# S2-045: SlideVideoMapping migration

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 2h → **Фактично:** no-op (документація)

---

## Мета

Існуючі маппінги мігровані на нову структуру

## Рішення: data migration не потрібна

Продакшн VPS був задеплоєний виключно для тестування.
На момент застосування міграції `a8f1e2c3d4b5` (S2-038)
в старій таблиці `slide_video_mappings` не було жодних
реальних даних. Міграція коректно використовує
`DROP TABLE` + `CREATE TABLE`.

Навіть за наявності даних, автоматична міграція була б
неможливою — стара схема (`course_id`, `slide_number`,
`video_timecode`) не містить інформації для визначення
`presentation_entry_id` та `video_entry_id` (нова схема).

**Детальна документація:** [`migration-details.md`](migration-details.md)

## Як тестуємо

**Автоматизовано:** `test_schema_sync` (integration, requires_db) —
перевіряє що ORM metadata = DB schema після всіх міграцій.

**Human control:** підтверджено відсутність production даних.

## Точки контролю

- [x] Міграція існує і працює (`a8f1e2c3d4b5`)
- [x] Відсутність production даних підтверджена
- [x] Tests зелені (`make check`)
- [x] Documentation checkpoint: повна документація в migration-details.md
