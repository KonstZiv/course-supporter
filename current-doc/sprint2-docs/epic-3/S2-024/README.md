# S2-024: FingerprintService — material level

**Epic:** EPIC-3 — Merkle Fingerprints
**Оцінка:** 2h

---

## Мета

Lazy cached sha256 від processed_content

## Що робимо

ensure_material_fp() — рахує і кешує content_fingerprint

## Як робимо

1. ensure_material_fp(entry) → sha256(processed_content)
2. Якщо content_fingerprint не None → повертає кешоване
3. flush після розрахунку

## Очікуваний результат

content_fingerprint розраховується лише раз до наступної інвалідації

## Як тестуємо

**Автоматизовано:** Unit test: calculate, cache hit, invalidation → recalculate

**Human control:** Немає (покривається unit tests)

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
