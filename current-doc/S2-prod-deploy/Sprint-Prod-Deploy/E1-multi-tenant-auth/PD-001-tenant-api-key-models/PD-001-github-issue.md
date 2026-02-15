# PD-001: Tenant & API Key ORM Models

## Що

Створити ORM-моделі `Tenant` та `APIKey` + Alembic міграцію. Це фундамент multi-tenant архітектури — всі наступні задачі auth залежать від цих моделей.

## Навіщо

Course Supporter — B2B API. Кожна компанія-клієнт (tenant) отримує свої API ключі з визначеними scopes та rate limits. Потрібна структура даних для зберігання tenants та їх ключів.

## Ключові рішення

- API key зберігається як SHA-256 hash (повний ключ показується тільки при створенні)
- Формат ключа: `cs_live_<32hex>` — prefix для ідентифікації в логах
- `key_prefix` зберігається окремо для пошуку без hash lookup
- `scopes` — JSON list (`["prep"]`, `["prep", "check"]`, `["prep", "check", "both"]`)
- Rate limits задаються per key, per scope

## Acceptance Criteria

- [ ] `Tenant` та `APIKey` моделі в `storage/orm.py`
- [ ] Alembic міграція створює обидві таблиці
- [ ] Unique constraint на `api_keys.key_hash`
- [ ] FK `api_keys.tenant_id → tenants.id` з CASCADE delete
- [ ] Тести: створення tenant, створення key, каскадне видалення
