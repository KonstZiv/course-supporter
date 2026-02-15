# Epic 1: Multi-tenant & Auth

## Мета

Фундамент для B2B API: ізоляція даних per tenant, API key автентифікація, rate limiting per service scope (prep/check). Після епіку — кожен запит до API прив'язаний до конкретного tenant-клієнта з перевіркою прав доступу до сервісу та обмеженням частоти запитів.

## Бізнес-контекст

Course Supporter — B2B API service. Компанії-клієнти (tenants) інтегрують його у свої LMS-платформи. Два сервіси білінгуються окремо:
- **prep** — підготовка курсу (ingestion, structuring)
- **check** — перевірка домашок, tracking студентів (Sprint 2+)

## Задачі

| ID | Назва | Залежності |
| :---- | :---- | :---- |
| PD-001 | Tenant & API Key ORM models | — |
| PD-002 | tenant_id на існуючих таблицях | PD-001 |
| PD-003 | API Key auth middleware | PD-001 |
| PD-004 | Service scope enforcement | PD-003 |
| PD-005 | Rate limiting middleware | PD-003 |
| PD-006 | Tenant-scoped repositories | PD-002, PD-003 |
| PD-007 | Admin CLI для управління tenants | PD-001 |

## Результат

- Кожен API запит вимагає `X-API-Key` header
- Запит без ключа → 401, з невалідним scope → 403, перевищення rate limit → 429
- Tenant A не бачить дані Tenant B
- `llm_calls.tenant_id` дозволяє білінг per tenant
- CLI скрипт створює tenants та видає ключі
