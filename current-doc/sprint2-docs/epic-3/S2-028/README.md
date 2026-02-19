# S2-028: Integration з MaterialEntry/Node repositories

**Epic:** EPIC-3 — Merkle Fingerprints
**Оцінка:** 2h

---

## Мета

Auto-invalidation при будь-яких CRUD операціях

## Що робимо

Hook invalidation в repository methods

## Як робимо

1. MaterialEntryRepository.update_source → invalidate entry fp + _invalidate_up
2. MaterialEntryRepository.complete_processing → invalidate entry fp + _invalidate_up
3. MaterialNodeRepository.move → invalidate old parent + new parent
4. MaterialNodeRepository.delete → invalidate parent

## Очікуваний результат

Fingerprints автоматично інвалідуються при будь-яких змінах

## Як тестуємо

**Автоматизовано:** Integration tests: CRUD operations → verify fingerprint invalidation

**Human control:** Немає

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
