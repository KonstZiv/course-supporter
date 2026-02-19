# S2-025: FingerprintService — node level (recursive)

**Epic:** EPIC-3 — Merkle Fingerprints
**Оцінка:** 3h

---

## Мета

Merkle hash для вузла: hash(material_fps + child_node_fps)

## Що робимо

ensure_node_fp() — рекурсивний розрахунок з кешуванням

## Як робимо

1. ensure_node_fp(node): sorted materials fps ('m:...') + sorted children fps ('n:...')
2. sha256 від joined parts
3. Рекурсія вниз по дереву
4. Кешування на кожному рівні

## Очікуваний результат

node_fingerprint = Merkle hash всього піддерева

## Як тестуємо

**Автоматизовано:** Unit tests: single node, nested 3 levels, deterministic (same data = same hash), empty node

**Human control:** Немає (покривається unit tests)

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
