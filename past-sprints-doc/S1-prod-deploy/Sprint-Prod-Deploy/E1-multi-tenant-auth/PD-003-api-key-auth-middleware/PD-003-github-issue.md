# PD-003: API Key Auth Middleware

## Що

FastAPI dependency для автентифікації через `X-API-Key` header. Перевіряє ключ, завантажує tenant, інжектить tenant context у request state.

## Навіщо

Кожен запит до API повинен бути автентифікований. Middleware — єдина точка входу для auth logic, endpoints отримують ready-to-use tenant context.

## Ключові рішення

- FastAPI `Depends()` dependency, не middleware — кращий контроль, type safety, testability
- Hash API key → lookup в DB → завантаження tenant
- Кешування результату на час запиту (один DB lookup per request)
- 401 без ключа, 401 з невалідним ключем, 401 з inactive tenant/key

## Acceptance Criteria

- [ ] `get_current_tenant` dependency у `api/deps.py`
- [ ] Запит без `X-API-Key` → 401
- [ ] Запит з невалідним ключем → 401
- [ ] Запит з inactive ключем або tenant → 401
- [ ] Запит з expired ключем → 401
- [ ] Валідний ключ → `request.state.tenant_id` та `request.state.scopes` встановлені
- [ ] Тести на всі сценарії
