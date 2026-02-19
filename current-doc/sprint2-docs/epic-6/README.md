# EPIC-6: Structure Generation Pipeline

**Оцінка:** 3-4 дні
**Ціль:** Per-node trigger, каскадна обробка, fingerprint check, snapshot, conflict detection.

---

## Задачі

- [S2-047: CourseStructureSnapshot ORM + repository](./S2-047/README.md) (3h)
- [S2-048: Subtree readiness check](./S2-048/README.md) (2h)
- [S2-049: Conflict detection (subtree overlap)](./S2-049/README.md) (3h)
- [S2-050: Generate structure ARQ task](./S2-050/README.md) (4h)
- [S2-051: Cascade generation orchestrator](./S2-051/README.md) (4h)
- [S2-052: Free vs Guided mode](./S2-052/README.md) (3h)
- [S2-053: Structure generation API](./S2-053/README.md) (4h)
- [S2-054: MergeStep refactor — tree-aware](./S2-054/README.md) (3h)
- [S2-055: Mapping warnings in generation](./S2-055/README.md) (2h)
- [S2-056: Structure generation tests](./S2-056/README.md) (4h)

---

## Автоматизований контроль результатів Epic

Integration tests: full pipeline (ingestion→generation), idempotency, conflict detection, cascade orchestration, free vs guided modes. Unit tests: subtree readiness, overlap detection (is_ancestor_or_same), snapshot CRUD.

---

## Ручний контроль результатів Epic (Human testing)

1. Курс з 3 nodes, всі READY → POST generate (free mode) → 202 → дочекатись → GET structure → snapshot є
2. Той самий fingerprint+mode → POST generate → 200 OK (idempotent)
3. Змінити 1 матеріал → POST generate → 202 (новий fingerprint)
4. POST generate для Node A (active) + POST generate для Node B → 202 (незалежні)
5. POST generate для Node A (active) + POST generate для Node A1 → 409 (overlap)
6. Node з RAW матеріалами → POST generate → 202 з планом ingestion
7. Перевірити guided mode — чи зберігає input tree structure

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
