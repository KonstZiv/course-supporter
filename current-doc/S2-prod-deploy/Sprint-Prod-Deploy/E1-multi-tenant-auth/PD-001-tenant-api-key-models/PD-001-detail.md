# PD-001: Tenant & API Key ORM Models — Detail ✅

## Контекст

Course Supporter обслуговує кілька компаній-клієнтів (tenants). Кожен tenant отримує API ключі для доступу до двох сервісів: підготовка курсів (prep) та перевірка домашок (check). Ця задача створює data model для multi-tenant auth.

## ORM Models

### Tenant

`src/course_supporter/storage/orm.py`:

```python
class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    api_keys: Mapped[list["APIKey"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
```

### APIKey

```python
class APIKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(100), default="default")
    scopes: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    rate_limit_prep: Mapped[int] = mapped_column(Integer, default=60)
    rate_limit_check: Mapped[int] = mapped_column(Integer, default=300)
    is_active: Mapped[bool] = mapped_column(default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="api_keys")
```

> **Примітка:** `scopes` має тип `Mapped[list[Any]]` (не `list[str]`) через обмеження mypy strict mode з JSONB.

## API Key Format

```
cs_live_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4
│  │    └── 32 chars = secrets.token_hex(16)
│  └── environment: live | test
└── prefix: "cs" (Course Supporter)
```

Утилітні функції в `src/course_supporter/auth/keys.py`:

```python
def generate_api_key(environment: str = "live") -> tuple[str, str, str]:
    """Generate API key, return (full_key, key_hash, key_prefix)."""
    random_part = secrets.token_hex(16)
    full_key = f"cs_{environment}_{random_part}"
    key_hash = hashlib.sha256(full_key.encode()).hexdigest()
    key_prefix = f"cs_{environment}_{random_part[:4]}"
    return full_key, key_hash, key_prefix


def hash_api_key(key: str) -> str:
    """Hash an API key for lookup."""
    return hashlib.sha256(key.encode()).hexdigest()
```

## Alembic Migration

Файл: `migrations/versions/bb847e98ee7b_add_tenants_and_api_keys.py`

Результат міграції:
- Таблиця `tenants` з полями id, name, is_active, created_at, updated_at
- Таблиця `api_keys` з полями id, tenant_id, key_hash, key_prefix, label, scopes, rate_limit_prep, rate_limit_check, is_active, expires_at, created_at
- Unique index на `api_keys.key_hash`
- FK `api_keys.tenant_id → tenants.id` ON DELETE CASCADE

## Структура файлів

```
src/course_supporter/
├── auth/
│   ├── __init__.py       # Public exports: TenantContext, generate_api_key, hash_api_key
│   └── keys.py           # Key generation and hashing utilities
└── storage/
    └── orm.py            # + Tenant, APIKey models
```

## Тести

Файл: `tests/unit/test_tenant_models.py` — **14 тестів**

Tenant ORM tests: create, unique name, relationships, is_active default, timestamps.
APIKey ORM tests: create, hash unique, cascade delete, scopes JSONB, rate limits defaults.
Key utility tests: format validation, uniqueness, hash determinism.

## Definition of Done

- [x] `Tenant` та `APIKey` моделі додані до `storage/orm.py`
- [x] `auth/keys.py` з `generate_api_key()` та `hash_api_key()`
- [x] Alembic міграція створює обидві таблиці
- [x] Unique constraint на `api_keys.key_hash`
- [x] FK з CASCADE delete
- [x] 14 тестів зелені
- [x] `make check` зелений
