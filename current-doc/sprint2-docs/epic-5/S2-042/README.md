# S2-042: Auto-revalidation on ingestion complete

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 3h

---

## Мета

Завершення ingestion автоматично перевалідовує pending маппінги

## Що робимо

Hook в ingestion callback (S2-009): find blocked mappings → revalidate

## Як робимо

1. find_blocked_by(material_entry_id) → list of pending mappings
2. Для кожного: recalculate blocking_factors
3. Якщо всі блокери зняті → run Level 2 validation
4. Update validation_state accordingly

## Очікуваний результат

Ingestion complete → pending маппінги автоматично валідуються

## Як тестуємо

**Автоматизовано:** Integration test: create mapping (pending) → ingestion completes → mapping becomes validated

**Human control:** Створити маппінг з pending презентацією → дочекатись ingestion → GET mapping → state = validated

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
