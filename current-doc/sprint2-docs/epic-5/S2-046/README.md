# S2-046: Mapping validation unit tests

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 4h

---

## Мета

Повне тестове покриття для валідації маппінгів

## Що робимо

Tests для всіх 3 рівнів валідації, auto-revalidation, partial success

## Як робимо

1. Level 1: wrong type, wrong node, invalid timecode
2. Level 2: slide range, timecode range, boundary values
3. Level 3: pending→validated lifecycle, pending→error lifecycle
4. Batch: partial success scenarios
5. Auto-revalidation: ingestion triggers

## Очікуваний результат

Повне покриття всіх validation scenarios

## Як тестуємо

**Автоматизовано:** pytest

**Human control:** Review — чи покриті edge cases з AR-7

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
