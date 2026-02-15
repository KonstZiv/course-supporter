# PD-004: Service Scope Enforcement — Detail

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

```python
# src/course_supporter/auth/scopes.py

from fastapi import Depends, HTTPException

from course_supporter.auth.context import TenantContext
from course_supporter.api.deps import get_current_tenant


def require_scope(*required_scopes: str):
    """Dependency factory: require at least one of the given scopes.

    Usage:
        @router.post("/courses", dependencies=[Depends(require_scope("prep"))])

    Or as parameter dependency:
        async def endpoint(tenant: TenantContext = Depends(require_scope("prep"))):
    """
    async def _check_scope(
        tenant: TenantContext = Depends(get_current_tenant),
    ) -> TenantContext:
        if not any(s in tenant.scopes for s in required_scopes):
            raise HTTPException(
                status_code=403,
                detail=f"Requires scope: {' or '.join(required_scopes)}",
            )
        return tenant

    return _check_scope
```

### Застосування на endpoints

```python
# src/course_supporter/api/routes/courses.py

@router.post(
    "/courses",
    status_code=201,
)
async def create_course(
    body: CourseCreateRequest,
    tenant: TenantContext = Depends(require_scope("prep")),
    session: AsyncSession = Depends(get_session),
) -> CourseResponse:
    ...

@router.get("/courses/{course_id}")
async def get_course(
    course_id: uuid.UUID,
    tenant: TenantContext = Depends(require_scope("prep", "check")),
    session: AsyncSession = Depends(get_session),
) -> CourseDetailResponse:
    ...
```

## Структура файлів

```
src/course_supporter/auth/
├── __init__.py       # + require_scope
├── context.py
├── keys.py
└── scopes.py         # require_scope factory
```

## Тести

Файл: `tests/unit/test_scope_enforcement.py`

1. **test_prep_scope_allows_prep_endpoint** — scope=["prep"], POST /courses → 201
2. **test_check_scope_denied_prep_endpoint** — scope=["check"], POST /courses → 403
3. **test_prep_scope_denied_check_endpoint** — scope=["prep"], POST /check-homework → 403
4. **test_shared_endpoint_allows_prep** — scope=["prep"], GET /courses/{id} → 200
5. **test_shared_endpoint_allows_check** — scope=["check"], GET /courses/{id} → 200
6. **test_both_scopes_tenant** — scope=["prep","check"] → доступ до всього

Очікувана кількість тестів: **6**

## Definition of Done

- [ ] `require_scope()` factory dependency
- [ ] Всі існуючі prep endpoints захищені
- [ ] Shared endpoints дозволяють обидва scopes
- [ ] 6 тестів зелені
- [ ] `make check` зелений
- [ ] Документ оновлений відповідно до фінальної реалізації
