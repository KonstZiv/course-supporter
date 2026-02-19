# S2-039: MappingValidationService — structural validation (Level 1)

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 3h

---

## Мета

Перевірка що матеріали існують, належать вузлу, мають правильний тип

## Що робимо

validate_structural(): node membership, source_type, timecode format

## Як робимо

1. Check entry exists and node_id matches
2. Check source_type = presentation/video
3. Validate timecode format (HH:MM:SS)
4. Detailed error messages з hints

## Очікуваний результат

Структурні помилки ловляться до створення маппінгу

## Як тестуємо

**Автоматизовано:** Unit tests: wrong node, wrong type, invalid timecode, missing entry

**Human control:** Подати маппінг з wrong type → перевірити що error message зрозумілий і корисний

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
