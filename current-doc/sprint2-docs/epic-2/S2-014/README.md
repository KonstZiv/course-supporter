# S2-014: MaterialEntry ORM model

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 3h

---

## Мета

ORM модель матеріалу з raw/processed layers і pending receipt

## Що робимо

Створити MaterialEntry з усіма полями: raw layer, processed layer, pending receipt, fingerprint

## Як робимо

1. MaterialEntry з усіма полями згідно AR-2
2. FK на MaterialNode (node_id) і Job (pending_job_id)
3. Relationships з MaterialNode і Job

## Очікуваний результат

MaterialEntry ORM працює, всі поля доступні

## Як тестуємо

**Автоматизовано:** Unit test: create entry, set/clear pending receipt, update processed layer

**Human control:** Перевірити в DB структуру таблиці — всі колонки відповідають AR-2

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
