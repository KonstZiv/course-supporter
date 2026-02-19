# EPIC-7: Integration Documentation

**Оцінка:** 1-2 дні
**Ціль:** Зовнішня команда може почати інтеграцію. Публікується на docs site (Epic 0).

---

## Задачі

- [S2-057: Flow Guide](./S2-057/README.md) (3h)
- [S2-058: API Reference update](./S2-058/README.md) (2h)
- [S2-059: Auth & onboarding guide](./S2-059/README.md) (1h)
- [S2-060: Error handling guide](./S2-060/README.md) (2h)

---

## Автоматизований контроль результатів Epic

mkdocs build --strict (перевірка broken links). OpenAPI schema validation.

---

## Ручний контроль результатів Epic (Human testing)

1. Дати документацію новій людині → чи може вона пройти повний flow без додаткових питань?
2. Перевірити всі приклади запитів — чи працюють copy-paste в curl/Postman
3. Перевірити error scenarios — чи документовані всі коди помилок

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
