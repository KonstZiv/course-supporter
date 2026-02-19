# S2-055: Mapping warnings in generation

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 2h

---

## Мета

Маппінги з pending_validation/validation_failed включаються як warnings

## Що робимо

При generation перевіряти validation_state маппінгів в scope

## Як робимо

1. Зібрати маппінги в піддереві
2. Якщо є pending_validation або validation_failed → warnings в response
3. Не блокує generation, лише інформує

## Очікуваний результат

Generation response включає warnings про problematic маппінги

## Як тестуємо

**Автоматизовано:** Unit test: generate з pending mappings → warnings in response

**Human control:** Перевірити що warnings зрозумілі і допомагають виправити проблему

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
