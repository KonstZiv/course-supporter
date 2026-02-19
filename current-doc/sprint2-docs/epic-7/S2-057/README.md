# S2-057: Flow Guide

**Epic:** EPIC-7 — Integration Documentation
**Оцінка:** 3h

---

## Мета

Повний сценарій від створення курсу до отримання структури

## Що робимо

docs/api/flow-guide.md — step-by-step guide з прикладами

## Як робимо

1. Описати кожен крок: create course → add nodes → upload materials → generate
2. Curl приклади для кожного endpoint
3. Описати polling pattern для async operations
4. Описати error recovery scenarios

## Очікуваний результат

Новий розробник може пройти повний flow за 30 хвилин

## Як тестуємо

**Автоматизовано:** Немає (контентна задача)

**Human control:** Дати guide новій людині — чи проходить flow без питань

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
