# EPIC-2: MaterialTree + MaterialEntry

**Оцінка:** 4-5 днів
**Ціль:** Recursive tree вузлів, MaterialEntry з raw/processed розділенням і квитанцією.

---

## Задачі

- [S2-013: MaterialNode ORM model](./S2-013/README.md) (2h)
- [S2-014: MaterialEntry ORM model](./S2-014/README.md) (3h)
- [S2-015: MaterialState derived property](./S2-015/README.md) (1h)
- [S2-016: MaterialNode repository](./S2-016/README.md) (4h)
- [S2-017: MaterialEntry repository](./S2-017/README.md) (4h)
- [S2-018: Alembic migration: new tables + data migration](./S2-018/README.md) (4h)
- [S2-019: Tree API endpoints (nodes)](./S2-019/README.md) (4h)
- [S2-020: Materials endpoint refactor](./S2-020/README.md) (3h)
- [S2-021: Course detail response — tree structure](./S2-021/README.md) (3h)
- [S2-022: List courses endpoint](./S2-022/README.md) (1h)
- [S2-023: Tree + MaterialEntry unit tests](./S2-023/README.md) (5h)

---

## Автоматизований контроль результатів Epic

Unit tests: MaterialNode CRUD, recursive fetch, move, cascade delete. MaterialEntry CRUD, state transitions (RAW→PENDING→READY→INTEGRITY_BROKEN), pending receipt lifecycle. API tests: endpoints return correct tree structure, tenant isolation, validation errors.

---

## Ручний контроль результатів Epic (Human testing)

1. Створити курс → додати root node → додати child nodes (3 рівні глибини)
2. Додати матеріали на різних рівнях (root, mid, leaf)
3. GET /courses/{id} → перевірити що дерево відображається правильно зі станами матеріалів
4. Перемістити node (change parent) → перевірити що дерево оновилось
5. Видалити node → перевірити cascade delete children + materials
6. Перевірити що інший tenant не бачить ці курси

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
