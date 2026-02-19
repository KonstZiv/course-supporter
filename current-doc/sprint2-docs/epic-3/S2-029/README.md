# S2-029: Fingerprint в API responses

**Epic:** EPIC-3 — Merkle Fingerprints
**Оцінка:** 2h

---

## Мета

Всі GET endpoints повертають fingerprints

## Що робимо

Додати fingerprint поля в response schemas

## Як робимо

1. CourseDetailResponse: course_fingerprint
2. NodeResponse: node_fingerprint
3. MaterialEntryResponse: content_fingerprint
4. fingerprint=null означає 'потребує перерахунку'

## Очікуваний результат

API response містить fingerprints на всіх рівнях

## Як тестуємо

**Автоматизовано:** API test: GET course → verify fingerprints present and correct

**Human control:** GET /courses/{id} — перевірити що fingerprints видимі на кожному рівні дерева

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
