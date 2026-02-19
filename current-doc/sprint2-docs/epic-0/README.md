# EPIC-0: Project Documentation Infrastructure

**Оцінка:** 1-2 дні
**Ціль:** Документація проєкту на GitHub Pages (mkdocs). ERD що оновлюється, структуровані описи спрінтів.

---

## Задачі

- [S2-000a: mkdocs setup + theme](./S2-000a/README.md) (2h)
- [S2-000b: GitHub Actions → GitHub Pages deploy](./S2-000b/README.md) (2h)
- [S2-000c: ERD page — Mermaid rendering](./S2-000c/README.md) (2h)
- [S2-000d: Sprint 1 — ретроспективний опис](./S2-000d/README.md) (3h)
- [S2-000e: Sprint 2 — поточний опис](./S2-000e/README.md) (2h)
- [S2-000f: Структура документації + landing](./S2-000f/README.md) (2h)
- [S2-000g: README оновлення](./S2-000g/README.md) (1h)

---

## Автоматизований контроль результатів Epic

Перевірка що mkdocs build проходить без помилок. CI pipeline green.

---

## Ручний контроль результатів Epic (Human testing)

Відкрити docs site в браузері, перевірити що: landing page відображається коректно, навігація працює, ERD рендериться як інтерактивна Mermaid-діаграма, Sprint 1 і Sprint 2 сторінки містять актуальну інформацію. Перевірити що push в main автоматично оновлює сайт.

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
