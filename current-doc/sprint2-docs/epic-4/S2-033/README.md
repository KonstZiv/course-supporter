# S2-033: Extract vision/slide description

**Epic:** EPIC-4 — Heavy Steps Extraction
**Оцінка:** 3h

---

## Мета

Vision API виклик — окрема injectable function

## Що робимо

Виділити vision виклик з PresentationProcessor

## Як робимо

Аналогічно S2-032, але для slide image → description через Gemini/GPT vision

## Очікуваний результат

local_describe_slides працює автономно

## Як тестуємо

**Автоматизовано:** Unit test з mock vision API

**Human control:** Ingestion презентації → результат ідентичний

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
