# S2-020: Materials endpoint refactor

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 3h

---

## Мета

Матеріали завантажуються через node (не напряму в course)

## Що робимо

Перенести POST materials на POST /nodes/{node_id}/materials, оновити DELETE і retry

## Як робимо

1. POST /courses/{id}/nodes/{node_id}/materials — додати матеріал до вузла
2. DELETE /courses/{id}/materials/{material_id} — видалити
3. POST /courses/{id}/materials/{material_id}/retry — повторити ingestion
4. При створенні: auto-enqueue ingestion job (S2-008)

## Очікуваний результат

Матеріали завантажуються в конкретний вузол, auto-ingestion працює

## Як тестуємо

**Автоматизовано:** API tests: upload to node, delete, retry, wrong node → 404, wrong tenant → 404

**Human control:** Завантажити файл в конкретний вузол → GET course → перевірити що матеріал в правильному вузлі

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
