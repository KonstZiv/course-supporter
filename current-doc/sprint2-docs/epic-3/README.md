# EPIC-3: Merkle Fingerprints

**Оцінка:** 2-3 дні
**Ціль:** Lazy cached fingerprints з каскадною інвалідацією знизу вгору.

---

## Задачі

- [S2-024: FingerprintService — material level](./S2-024/README.md) (2h)
- [S2-025: FingerprintService — node level (recursive)](./S2-025/README.md) (3h)
- [S2-026: FingerprintService — course level](./S2-026/README.md) (1h)
- [S2-027: Cascade invalidation (_invalidate_up)](./S2-027/README.md) (2h)
- [S2-028: Integration з MaterialEntry/Node repositories](./S2-028/README.md) (2h)
- [S2-029: Fingerprint в API responses](./S2-029/README.md) (2h)
- [S2-030: Fingerprint unit tests](./S2-030/README.md) (3h)

---

## Автоматизований контроль результатів Epic

Unit tests: Merkle hash correctness (детерміновані значення), cascade invalidation, lazy calculation (ensure_*_fp), independence гілок, порожній вузол.

---

## Ручний контроль результатів Epic (Human testing)

1. Створити курс з деревом матеріалів → GET course → fingerprints на всіх рівнях
2. Змінити один матеріал → GET course → побачити null fingerprints від точки зміни до кореня
3. Перевірити що незмінені гілки зберігають свої fingerprints
4. Trigger ensure_node_fp → fingerprints перераховані

---

## Обов'язкові дії після завершення Epic

1. **Оновити ERD** на docs site якщо змінились моделі
2. **Оновити Sprint 2 progress** на docs site
3. **Ревізія наступних epic-ів** — чи не вплинули зміни на їх scope
4. **Оновити task documents** для наступних epic-ів якщо з'явились нові залежності або зміни в підході
5. **PR review checklist:**
   - [ ] Код відповідає архітектурним рішенням (AR-*)
   - [ ] Unit tests додані/оновлені
   - [ ] Error messages зрозумілі і містять hints
   - [ ] Документація оновлена
