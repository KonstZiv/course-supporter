# PD-003: API Key Auth Middleware — Detail ✅

## Контекст

Після PD-001 (ORM models) та PD-002 (tenant_id на таблицях), потрібен механізм автентифікації запитів. Кожен запит повинен містити API key, який визначає tenant та його права.

## Auth Flow

```
Request
  → X-API-Key header extraction
  → SHA-256 hash
  → DB: SELECT api_keys JOIN tenants WHERE key_hash = ? AND api_key.is_active AND tenant.is_active
  → Check expires_at
  → Return TenantContext
  → Endpoint
```

## Реалізація

### TenantContext dataclass

`src/course_supporter/auth/context.py`:

```python
@dataclass(frozen=True)
class TenantContext:
    """Authenticated tenant context, injected into every request."""
    tenant_id: uuid.UUID
    tenant_name: str
    scopes: list[str]
    rate_limit_prep: int
    rate_limit_check: int
    key_prefix: str
```

### FastAPI Dependency

`src/course_supporter/api/deps.py`:

```python
api_key_header = APIKeyHeader(name="X-API-Key")

# Module-level to avoid ruff B008 (function call in default argument)
_get_session = Depends(get_session)


async def get_current_tenant(
    api_key: str = Security(api_key_header),
    session: AsyncSession = _get_session,
) -> TenantContext:
    """Authenticate request via API key, return tenant context.

    Raises:
        HTTPException 401: Invalid API key (not found, inactive tenant/key).
        HTTPException 401: API key expired.
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

### Health endpoint — без auth

```python
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
```

`/health` та `/docs` не використовують `Depends(get_current_tenant)` — працюють без auth.

### Swagger UI integration

`APIKeyHeader` автоматично додає "Authorize" кнопку в Swagger UI.

## Структура файлів

```
src/course_supporter/auth/
├── __init__.py           # Exports: TenantContext, generate_api_key, hash_api_key
│                         # NOTE: require_scope NOT exported (circular import avoidance)
├── context.py            # TenantContext dataclass
└── keys.py               # generate_api_key, hash_api_key

src/course_supporter/api/
└── deps.py               # get_current_tenant, get_session, get_model_router, get_s3_client
```

> **Важливо:** `require_scope` НЕ re-exported з `auth/__init__.py` щоб уникнути circular import: `auth/__init__` → `auth/scopes` → `api/deps` → `auth/context`.

## Тести

Файл: `tests/unit/test_auth_middleware.py` — **8 тестів**

1. `test_missing_api_key_returns_401` — запит без header → 401
2. `test_invalid_api_key_returns_401` — неіснуючий ключ → 401
3. `test_inactive_api_key_returns_401` — ключ з `is_active=False` → 401
4. `test_inactive_tenant_returns_401` — tenant з `is_active=False` → 401
5. `test_expired_api_key_returns_401` — expired key → 401 "API key expired"
6. `test_valid_api_key_returns_200` — валідний ключ → 200
7. `test_tenant_context_fields` — всі поля TenantContext правильні
8. `test_health_no_auth_required` — `/health` працює без ключа

## Definition of Done

- [x] `TenantContext` frozen dataclass
- [x] `get_current_tenant` dependency з SHA-256 lookup
- [x] `APIKeyHeader` для Swagger UI
- [x] Health endpoint без auth
- [x] `_get_session` module-level (ruff B008)
- [x] 8 тестів зелені
- [x] `make check` зелений
