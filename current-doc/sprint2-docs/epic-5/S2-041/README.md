# S2-041: MappingValidationService — deferred validation (Level 3)

**Epic:** EPIC-5 — SlideVideoMapping — Redesign
**Оцінка:** 4h

---

## Мета

Маппінги приймаються коли матеріали ще не оброблені, валідуються пізніше

## Що робимо

Deferred validation: blocking_factors JSONB, PENDING_VALIDATION state

## Як робимо

1. Якщо матеріал не READY → записати blocking_factor
2. blocking_factor: type, material_entry_id, filename, state, message, blocked_checks
3. validation_state = pending_validation
4. Коли матеріал стає READY → зняти blocker → спробувати Level 2 валідацію

## Очікуваний результат

Маппінги з необробленими матеріалами приймаються з чітким описом що блокує

## Як тестуємо

**Автоматизовано:** Unit tests: create with pending material → blocking_factor, material becomes READY → validated, material ERROR → blocker updated

**Human control:** Подати маппінг з pending матеріалом → перевірити blocking_factors JSON — чи зрозумілий message

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
