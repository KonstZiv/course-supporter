# PD-004: Service Scope Enforcement — Detail ✅

## Контекст

Після PD-003 (auth middleware), `TenantContext` містить `scopes: list[str]`. Тепер потрібно перевіряти scope на рівні endpoint.

## Scope Mapping

```python
# Prep scope — course preparation:
POST   /api/v1/courses                         → prep
POST   /api/v1/courses/{id}/materials           → prep
POST   /api/v1/courses/{id}/slide-mapping       → prep

# Check scope — homework checking (Sprint 2+):
POST   /api/v1/courses/{id}/check-homework      → check
GET    /api/v1/students/{id}/progress            → check

# Shared — both scopes:
GET    /api/v1/courses/{id}                      → prep | check
GET    /api/v1/courses/{id}/lessons/{id}         → prep | check
GET    /api/v1/reports/cost                      → prep | check

# No auth:
GET    /health
GET    /docs
```

## Реалізація

### Scope dependency factory

`src/course_supporter/auth/scopes.py`:

```python
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends, HTTPException

from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.auth.rate_limiter import InMemoryRateLimiter

_tenant_dep = Depends(get_current_tenant)

# Global rate limiter instance (single-process; replace with Redis for scaling)
rate_limiter = InMemoryRateLimiter(window_seconds=60)


def require_scope(
    *required_scopes: str,
) -> Callable[..., Coroutine[Any, Any, TenantContext]]:
    """Dependency factory: require at least one scope, then enforce rate limit.

    Usage as parameter dependency (returns TenantContext)::

        async def endpoint(
            tenant: TenantContext = Depends(require_scope("prep")),
        ): ...

    Raises:
        HTTPException 403: if tenant has none of the required scopes.
        HTTPException 429: if rate limit exceeded (includes Retry-After header).
    """

    async def _check_scope(
        tenant: TenantContext = _tenant_dep,
    ) -> TenantContext:
        # 1. Scope check — next() returns matched scope for rate limit lookup
        matched_scope = next((s for s in required_scopes if s in tenant.scopes), None)
        if matched_scope is None:
            raise HTTPException(
                status_code=403,
                detail=f"Requires scope: {' or '.join(required_scopes)}",
            )

        # 2. Rate limit check (added in PD-005)
        ...

        return tenant

    return _check_scope
```

> **Ключові рішення:**
> - **`next()` замість `any()`** — повертає конкретний matched scope, який потрібен для вибору rate limit (prep vs check) в PD-005.
> - **`_tenant_dep = Depends(get_current_tenant)`** — module-level для уникнення ruff B008.
> - **`require_scope` НЕ re-exported** з `auth/__init__.py` — circular import: `auth/__init__` → `auth/scopes` → `api/deps` → `auth/context`.

### Annotated Dependencies

`src/course_supporter/api/routes/courses.py`:

```python
from typing import Annotated

PrepDep = Annotated[TenantContext, Depends(require_scope("prep"))]
SharedDep = Annotated[TenantContext, Depends(require_scope("prep", "check"))]


@router.post("/courses", status_code=201)
async def create_course(
    body: CourseCreateRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> CourseResponse:
    ...


@router.get("/courses/{course_id}")
async def get_course(
    course_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> CourseDetailResponse:
    ...
```

> **`Annotated` deps** — `PrepDep`, `SharedDep` замість `Depends(require_scope(...))` в кожному endpoint. Типізовані, чистіші сигнатури.

## Структура файлів

```
src/course_supporter/auth/
├── __init__.py       # Exports: TenantContext, generate_api_key, hash_api_key
│                     # NOTE: require_scope NOT exported (circular import avoidance)
├── context.py
├── keys.py
├── rate_limiter.py
└── scopes.py         # require_scope factory + rate_limiter instance
```

## Тести

Файл: `tests/unit/test_scope_enforcement.py` — **6 тестів**

1. `test_prep_scope_allows_prep_endpoint` — scope=["prep"], POST /courses → 201
2. `test_check_scope_denied_prep_endpoint` — scope=["check"], POST /courses → 403
3. `test_prep_scope_denied_check_only_endpoint` — scope=["prep"], temp FastAPI app з check-only route → 403
4. `test_shared_endpoint_allows_prep` — scope=["prep"], GET /courses/{id} → 200
5. `test_shared_endpoint_allows_check` — scope=["check"], GET /courses/{id} → 200
6. `test_both_scopes_tenant` — scope=["prep","check"] → доступ до всього

## Definition of Done

- [x] `require_scope()` factory dependency з `next()` scope matching
- [x] `Annotated` типи `PrepDep`, `SharedDep` на routes
- [x] Всі існуючі prep endpoints захищені
- [x] Shared endpoints дозволяють обидва scopes
- [x] `require_scope` НЕ re-exported з `auth/__init__.py`
- [x] 6 тестів зелені
- [x] `make check` зелений
