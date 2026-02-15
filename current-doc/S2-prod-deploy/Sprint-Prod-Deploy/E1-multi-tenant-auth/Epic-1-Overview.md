# Epic 1: Multi-tenant & Auth ✅

## Мета

Фундамент для B2B API: ізоляція даних per tenant, API key автентифікація, rate limiting per service scope (prep/check). Після епіку — кожен запит до API прив'язаний до конкретного tenant-клієнта з перевіркою прав доступу до сервісу та обмеженням частоти запитів.

## Бізнес-контекст

Course Supporter — B2B API service. Компанії-клієнти (tenants) інтегрують його у свої LMS-платформи. Два сервіси білінгуються окремо:
- **prep** — підготовка курсу (ingestion, structuring)
- **check** — перевірка домашок, tracking студентів (Sprint 2+)

## Задачі

| ID | Назва | Залежності | Статус | Тести |
| :---- | :---- | :---- | :---- | :---- |
| PD-001 | Tenant & API Key ORM models | — | ✅ | 14 |
| PD-002 | tenant_id на існуючих таблицях | PD-001 | ✅ | 8 |
| PD-003 | API Key auth middleware | PD-001 | ✅ | 8 |
| PD-004 | Service scope enforcement | PD-003 | ✅ | 6 |
| PD-005 | Rate limiting middleware | PD-003 | ✅ | 7 |
| PD-006 | Tenant-scoped repositories | PD-002, PD-003 | ✅ | 6 |
| PD-007 | Admin CLI для управління tenants | PD-001 | ✅ | 7 |

**Всього тестів Epic 1: 56**

## Результат

- ✅ Кожен API запит вимагає `X-API-Key` header
- ✅ Запит без ключа → 401, з невалідним scope → 403, перевищення rate limit → 429
- ✅ Tenant A не бачить дані Tenant B (repository-level isolation)
- ✅ `llm_calls.tenant_id` (nullable) дозволяє білінг per tenant
- ✅ CLI скрипт створює tenants та видає ключі

## Ключові архітектурні рішення

- **`require_scope` НЕ re-exported** з `auth/__init__.py` — уникнення circular import (auth → scopes → api.deps → auth)
- **`LLMCall.tenant_id` nullable** — global ModelRouter не має tenant context (background tasks, evals)
- **InMemoryRateLimiter** — single-process, для multi-instance → Redis
- **`asyncio.to_thread`** для cleanup rate limiter — не блокує event loop
- **`Annotated` deps** — `PrepDep`, `SharedDep` для типізованих scope dependencies
- **`next()` для scope matching** — замість `any()`, повертає конкретний matched scope для rate limit lookup
