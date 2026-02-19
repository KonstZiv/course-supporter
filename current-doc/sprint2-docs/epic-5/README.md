# EPIC-5: SlideVideoMapping — Redesign

**Оцінка:** 3-4 дні
**Ціль:** Explicit presentation↔video references, трирівнева валідація, deferred validation з auto-revalidation.

---

## Задачі

- [S2-038: SlideVideoMapping ORM redesign](./S2-038/README.md) (3h)
- [S2-039: MappingValidationService — structural validation (Level 1)](./S2-039/README.md) (3h)
- [S2-040: MappingValidationService — content validation (Level 2)](./S2-040/README.md) (3h)
- [S2-041: MappingValidationService — deferred validation (Level 3)](./S2-041/README.md) (4h)
- [S2-042: Auto-revalidation on ingestion complete](./S2-042/README.md) (3h)
- [S2-043: Batch create endpoint (partial success)](./S2-043/README.md) (4h)
- [S2-044: Mapping CRUD endpoints](./S2-044/README.md) (2h)
- [S2-045: SlideVideoMapping migration](./S2-045/README.md) (2h)
- [S2-046: Mapping validation unit tests](./S2-046/README.md) (4h)

---

## Автоматизований контроль результатів Epic

Unit tests: structural validation (wrong type, wrong node), content validation (slide out of range, timecode overflow), deferred validation lifecycle, auto-revalidation trigger, batch partial success, duplicate warnings.

---

## Ручний контроль результатів Epic (Human testing)

1. Створити node з 2 відео і 2 презентаціями → batch upload 10 маппінгів
2. Перевірити partial success: 8 створені, 2 з помилками (hints в response)
3. Маппінг з необробленою презентацією → pending_validation з blocking_factors
4. Дочекатись ingestion презентації → маппінг автоматично став validated
5. Ingestion зафейлив → маппінг має blocking_factor type=material_error
6. Перевірити error messages — чи зрозумілі і корисні

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
