# S2-027: Cascade invalidation (_invalidate_up)

**Epic:** EPIC-3 — Merkle Fingerprints
**Оцінка:** 2h

---

## Мета

Зміна матеріалу → інвалідація fingerprints від точки зміни до кореня

## Що робимо

_invalidate_up(node_id) — скидає node_fingerprint від node до root

## Як робимо

1. While node is not None: node.node_fingerprint = None; node = node.parent
2. flush після циклу
3. Інтегрувати в MaterialEntry модифікації (auto-invalidation)

## Очікуваний результат

Зміна leaf → всі ancestor nodes мають fingerprint=None

## Як тестуємо

**Автоматизовано:** Unit test: change leaf material → verify all ancestors invalidated, siblings untouched

**Human control:** Немає

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
