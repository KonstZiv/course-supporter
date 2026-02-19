# S2-043: Batch create endpoint (partial success)

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 4h

---

## Мета

POST batch маппінгів з per-item результатами і hints

## Що робимо

POST /courses/{id}/nodes/{node_id}/slide-mapping з array of mappings

## Як робимо

1. Accept array of mapping objects
2. Validate each independently
3. Create valid ones, skip invalid
4. Return per-item results: status (created/failed), errors, warnings
5. Response hints: resubmit guidance, batch_size recommendation

## Очікуваний результат

Batch upload з partial success і детальними per-item повідомленнями

## Як тестуємо

**Автоматизовано:** API tests: full success, partial success, full failure, empty batch

**Human control:** Подати batch 10 маппінгів (8 ok, 2 invalid) → перевірити response — per-item statuses, error hints

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
