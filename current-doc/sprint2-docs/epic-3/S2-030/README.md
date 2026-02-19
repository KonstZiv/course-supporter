# S2-030: Fingerprint unit tests

**Epic:** EPIC-3 — Merkle Fingerprints
**Оцінка:** 3h

---

## Мета

Повне тестове покриття Merkle fingerprints

## Що робимо

Comprehensive tests для FingerprintService

## Як робимо

1. Merkle correctness: known inputs → known hash
2. Cascade invalidation: change deep leaf → verify path to root
3. Independence: change in branch A doesn't affect branch B
4. Lazy calculation: ensure only calculates when needed
5. Edge: empty node, single material, very deep tree

## Очікуваний результат

Всі fingerprint тести зелені

## Як тестуємо

**Автоматизовано:** pytest

**Human control:** Review — чи покриті всі сценарії з AR-4

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
