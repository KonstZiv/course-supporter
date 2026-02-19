# S2-034: Extract web scraping

**Epic:** EPIC-4 — Heavy Steps Extraction
**Оцінка:** 2h

---

## Мета

Web scraping — окрема injectable function

## Що робимо

Виділити web fetch/parse з WebProcessor

## Як робимо

Аналогічно S2-032, але для URL → parsed content

## Очікуваний результат

local_scrape_web працює автономно

## Як тестуємо

**Автоматизовано:** Unit test з mock HTTP

**Human control:** Ingestion web URL → результат ідентичний

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
