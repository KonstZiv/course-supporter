# S2-021: Course detail response — tree structure

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 3h

---

## Мета

GET /courses/{id} повертає повне дерево з матеріалами і fingerprints

## Що робимо

Оновити CourseResponse schema — включити MaterialTree з nested nodes, materials, states

## Як робимо

1. CourseDetailResponse schema з nested NodeResponse
2. NodeResponse: id, title, children[], materials[], fingerprint
3. MaterialEntryResponse: id, filename, state, fingerprint, pending info
4. Recursive serialization дерева

## Очікуваний результат

GET /courses/{id} повертає повне дерево зі станами і fingerprints

## Як тестуємо

**Автоматизовано:** API test: create tree with materials → GET → verify full structure in response

**Human control:** GET /courses/{id} — JSON response має правильну вкладеність, стани матеріалів коректні

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
