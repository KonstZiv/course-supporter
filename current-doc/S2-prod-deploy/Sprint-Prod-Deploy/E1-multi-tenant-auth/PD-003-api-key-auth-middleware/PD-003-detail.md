# PD-003: API Key Auth Middleware — Detail

## Контекст

Після PD-001 (ORM models) та PD-002 (tenant_id на таблицях), потрібен механізм автентифікації запитів. Кожен запит повинен містити API key, який визначає tenant та його права.

## Auth Flow

```
Request
  → X-API-Key header extraction
  → SHA-256 hash
  → DB: SELECT api_keys JOIN tenants WHERE key_hash = ? AND api_key.is_active AND tenant.is_active
  → Check expires_at
  → Set request.state: tenant_id, scopes, rate_limits
  → Endpoint
```

## Реалізація

### Pydantic model для auth context

```python
# src/course_supporter/auth/context.py

from dataclasses import dataclass
import uuid


@dataclass(frozen=True)
class TenantContext:
    """Authenticated tenant context, injected into every request."""
    tenant_id: uuid.UUID
    tenant_name: str
    scopes: list[str]
    rate_limit_prep: int
    rate_limit_check: int
    key_prefix: str  # для логування
```

### FastAPI Dependency

```python
# src/course_supporter/api/deps.py — доповнення

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader

from course_supporter.auth.context import TenantContext
from course_supporter.auth.keys import hash_api_key

api_key_header = APIKeyHeader(name="X-API-Key")


async def get_current_tenant(
    api_key: str = Security(api_key_header),
    session: AsyncSession = Depends(get_session),
) -> TenantContext:
    """Authenticate request via API key, return tenant context.

    Raises:
        HTTPException 401: missing, invalid, inactive, or expired key.
    """
    key_hash = hash_api_key(api_key)

    stmt = (
        select(APIKey)
        .join(Tenant, APIKey.tenant_id == Tenant.id)
        .where(
            APIKey.key_hash == key_hash,
            APIKey.is_active.is_(True),
            Tenant.is_active.is_(True),
        )
        .options(selectinload(APIKey.tenant))
    )
    result = await session.execute(stmt)
    api_key_record = result.scalar_one_or_none()

    if api_key_record is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    if (
        api_key_record.expires_at is not None
        and api_key_record.expires_at < datetime.now(UTC)
    ):
        raise HTTPException(status_code=401, detail="API key expired")

    return TenantContext(
        tenant_id=api_key_record.tenant_id,
        tenant_name=api_key_record.tenant.name,
        scopes=api_key_record.scopes,
        rate_limit_prep=api_key_record.rate_limit_prep,
        rate_limit_check=api_key_record.rate_limit_check,
        key_prefix=api_key_record.key_prefix,
    )
```

### Використання в endpoints

```python
@router.post("/courses")
async def create_course(
    body: CourseCreateRequest,
    tenant: TenantContext = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> CourseResponse:
    repo = CourseRepository(session, tenant_id=tenant.tenant_id)
    ...
```

### Health endpoint — без auth

```python
@app.get("/health")
async def health() -> dict[str, str]:
    """Health check — no auth required."""
    return {"status": "ok"}
```

`/health` та `/docs` виключені з auth — вони не використовують `Depends(get_current_tenant)`.

### Swagger UI integration

`APIKeyHeader` автоматично додає "Authorize" кнопку в Swagger UI. Користувач вводить ключ один раз, далі всі запити йдуть з ним.

## Структура файлів

```
src/course_supporter/auth/
├── __init__.py           # Public: TenantContext, get_current_tenant
├── context.py            # TenantContext dataclass
└── keys.py               # generate_api_key, hash_api_key (з PD-001)

src/course_supporter/api/
└── deps.py               # + get_current_tenant dependency
```

## Тести

Файл: `tests/unit/test_auth_middleware.py`

Використовуємо `httpx.AsyncClient` + FastAPI `TestClient` з мокнутою DB session.

1. **test_missing_api_key_header** — запит без header → 401
2. **test_invalid_api_key** — запит з неіснуючим ключем → 401
3. **test_inactive_api_key** — ключ з `is_active=False` → 401
4. **test_inactive_tenant** — tenant з `is_active=False` → 401
5. **test_expired_api_key** — ключ з `expires_at` в минулому → 401
6. **test_valid_api_key** — валідний ключ → 200, tenant context правильний
7. **test_tenant_context_fields** — перевірка всіх полів TenantContext
8. **test_health_no_auth** — `/health` працює без ключа

Очікувана кількість тестів: **8**

## Definition of Done

- [ ] `TenantContext` dataclass
- [ ] `get_current_tenant` dependency
- [ ] `APIKeyHeader` для Swagger UI
- [ ] Health endpoint без auth
- [ ] 8 тестів зелені
- [ ] `make check` зелений
- [ ] Документ оновлений відповідно до фінальної реалізації
