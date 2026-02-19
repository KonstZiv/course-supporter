# S2-018: Alembic migration: new tables + data migration

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 4h

---

## Мета

Database schema оновлена, існуючі дані мігровані

## Що робимо

Alembic migration: створити material_nodes, material_entries, перенести дані з source_materials

## Як робимо

1. Migration: CREATE material_nodes, material_entries
2. Data migration: для кожного Course створити root MaterialNode
3. Перенести source_materials → material_entries (через root node)
4. Downgrade: зворотна міграція
5. Тестувати на копії production DB

## Очікуваний результат

alembic upgrade head працює, дані збережені, downgrade працює

## Як тестуємо

**Автоматизовано:** Migration test: upgrade → verify data → downgrade → verify rollback

**Human control:** Запустити міграцію на staging з копією production даних, перевірити що всі матеріали на місці

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
