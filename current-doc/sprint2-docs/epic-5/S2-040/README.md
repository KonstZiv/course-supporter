# S2-040: MappingValidationService — content validation (Level 2)

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 3h

---

## Мета

Перевірка slide_number і timecode в межах реального контенту

## Що робимо

validate_content(): slide range, timecode range (коли матеріал READY)

## Як робимо

1. Extract slide_count з processed_content → перевірити slide_number
2. Extract duration з processed_content → перевірити timecodes
3. Error messages з допустимими діапазонами

## Очікуваний результат

Контентні помилки ловляться коли матеріали оброблені

## Як тестуємо

**Автоматизовано:** Unit tests: slide out of range, timecode overflow, boundary values

**Human control:** Подати маппінг slide=100 для презентації з 30 слайдів → перевірити hint '1–30'

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
