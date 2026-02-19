# S2-058: API Reference update

**Epic:** EPIC-7 — Integration Documentation
**Оцінка:** 2h

---

## Мета

Повна API reference з прикладами запитів і відповідей

## Що робимо

docs/api/reference.md — всі endpoints з деталями

## Як робимо

1. Зібрати всі endpoints з OpenAPI schema
2. Додати приклади request/response для кожного
3. Описати query parameters, headers, body schemas

## Очікуваний результат

Повна API reference на docs site

## Як тестуємо

**Автоматизовано:** OpenAPI schema validation

**Human control:** Перевірити кожен приклад — чи працює copy-paste

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
