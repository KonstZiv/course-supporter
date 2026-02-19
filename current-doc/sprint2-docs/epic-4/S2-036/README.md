# S2-036: Factory for heavy steps

**Epic:** EPIC-4 — Heavy Steps Extraction
**Оцінка:** 2h

---

## Мета

Єдина точка створення heavy step implementations

## Що робимо

create_heavy_steps(settings) → dict з local implementations

## Як робимо

1. Factory function: повертає TranscribeFunc, DescribeSlidesFunc, etc.
2. Зараз: local implementations
3. Потім: switch на lambda implementations по settings flag

## Очікуваний результат

Один рядок змінює local → lambda для всіх heavy steps

## Як тестуємо

**Автоматизовано:** Unit test: factory повертає callable-и з правильними signatures

**Human control:** Немає

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
