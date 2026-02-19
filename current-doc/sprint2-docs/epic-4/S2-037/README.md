# S2-037: Heavy steps unit tests

**Epic:** EPIC-4 — Heavy Steps Extraction
**Оцінка:** 3h

---

## Мета

Повне тестове покриття для нової архітектури processors

## Що робимо

Tests для DI, orchestration, contract compliance

## Як робимо

1. Processor + mock heavy step → correct SourceDocument
2. Processor handles heavy step failure gracefully
3. Factory returns correct implementations
4. Type checking (mypy strict)

## Очікуваний результат

Всі тести зелені, mypy strict проходить

## Як тестуємо

**Автоматизовано:** pytest + mypy

**Human control:** Немає

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
