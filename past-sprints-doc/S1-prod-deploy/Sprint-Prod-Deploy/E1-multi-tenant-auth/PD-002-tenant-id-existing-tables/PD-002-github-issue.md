# PD-002: tenant_id на існуючих таблицях

## Що

Додати `tenant_id` FK до таблиць `courses` та `llm_calls`. Alembic міграція з backfill стратегією для існуючих записів.

## Навіщо

Ізоляція даних: tenant A не повинен бачити курси та LLM-витрати tenant B. `llm_calls.tenant_id` — основа для білінгу per tenant.

## Ключові рішення

- `tenant_id` NOT NULL — кожен запис належить tenant
- Існуючі записи: міграція створює "system" tenant, прив'язує до нього
- Index на `courses.tenant_id` та `llm_calls.tenant_id` для ефективної фільтрації

## Acceptance Criteria

- [ ] `courses.tenant_id` FK → tenants.id
- [ ] `llm_calls.tenant_id` FK → tenants.id
- [ ] Alembic міграція з backfill
- [ ] Індекси на нових FK columns
- [ ] Тести: створення course з tenant_id, фільтрація по tenant
