# PD-007: Admin CLI для управління tenants — Detail ✅

## Контекст

Потрібен інструмент для onboarding нових клієнтів та управління API ключами без UI. CLI з subcommands.

## CLI Interface

```bash
# Створити tenant
uv run python -m scripts.manage_tenant create-tenant --name "Python Academy"

# Видати API key
uv run python -m scripts.manage_tenant create-key \
    --tenant "Python Academy" \
    --scopes prep,check \
    --label production \
    --rate-prep 60 \
    --rate-check 300

# Output:
# API key created for "Python Academy":
#    Key:     cs_live_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4
#    Prefix:  cs_live_a1b2
#    Scopes:  prep, check
#    Label:   production
#
# Save this key now -- it cannot be retrieved later!

# Список tenants
uv run python -m scripts.manage_tenant list-tenants

# Output:
# Tenants:
#   1. Python Academy (active, 2 keys)
#   2. DevOps School (active, 1 key)

# Список ключів tenant
uv run python -m scripts.manage_tenant list-keys --tenant "Python Academy"

# Output:
# Keys for "Python Academy":
#   1. cs_live_a1b2 [production] scopes=prep,check active
#   2. cs_live_x9y8 [staging]    scopes=prep       revoked

# Відкликати ключ
uv run python -m scripts.manage_tenant revoke-key --prefix cs_live_a1b2

# Деактивувати tenant (всі ключі стають невалідними)
uv run python -m scripts.manage_tenant deactivate-tenant --name "Python Academy"
```

## Реалізація

`scripts/manage_tenant.py`:

```python
"""CLI for tenant and API key management.

Usage::

    uv run python -m scripts.manage_tenant <command> [options]

Commands:
    create-tenant       Create a new tenant
    create-key          Generate an API key for a tenant
    list-tenants        List all tenants
    list-keys           List API keys for a tenant
    revoke-key          Revoke an API key by prefix
    deactivate-tenant   Deactivate a tenant (all keys become invalid)
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from course_supporter.auth.keys import generate_api_key
from course_supporter.config import settings
from course_supporter.storage.orm import APIKey, Tenant


def get_sync_session() -> Session:
    """Create sync session for CLI operations.

    Uses the same database URL as the async app (psycopg v3
    handles both sync and async natively).
    """
    engine = create_engine(settings.database_url)
    return Session(engine)


def create_tenant(args: argparse.Namespace) -> None:
    """Create a new tenant."""
    with get_sync_session() as session:
        # Check for duplicates
        existing = session.execute(
            select(Tenant).where(Tenant.name == args.name)
        ).scalar_one_or_none()
        if existing is not None:
            print(f"Tenant already exists: {args.name}", file=sys.stderr)
            sys.exit(1)

        tenant = Tenant(name=args.name)
        session.add(tenant)
        session.commit()
        print(f"Tenant created: {args.name} (id: {tenant.id})")


def create_key(args: argparse.Namespace) -> None:
    """Generate an API key for a tenant."""
    with get_sync_session() as session:
        tenant = session.execute(
            select(Tenant).where(Tenant.name == args.tenant)
        ).scalar_one_or_none()
        if tenant is None:
            print(f"Tenant not found: {args.tenant}", file=sys.stderr)
            sys.exit(1)

        scopes = [s.strip() for s in args.scopes.split(",")]
        full_key, key_hash, key_prefix = generate_api_key()

        api_key = APIKey(
            tenant_id=tenant.id,
            key_hash=key_hash,
            key_prefix=key_prefix,
            label=args.label,
            scopes=scopes,
            rate_limit_prep=args.rate_prep,
            rate_limit_check=args.rate_check,
        )
        session.add(api_key)
        session.commit()

        print(f'API key created for "{args.tenant}":')
        print(f"   Key:     {full_key}")
        print(f"   Prefix:  {key_prefix}")
        print(f"   Scopes:  {', '.join(scopes)}")
        print(f"   Label:   {args.label}")
        print()
        print("Save this key now -- it cannot be retrieved later!")


def list_tenants(_args: argparse.Namespace) -> None:
    """List all tenants with key counts."""
    with get_sync_session() as session:
        stmt = (
            select(
                Tenant.name,
                Tenant.is_active,
                func.count(APIKey.id).label("key_count"),
            )
            .outerjoin(APIKey, Tenant.id == APIKey.tenant_id)
            .group_by(Tenant.id)
            .order_by(Tenant.name)
        )
        rows = session.execute(stmt).all()

        if not rows:
            print("No tenants found.")
            return

        print("Tenants:")
        for i, row in enumerate(rows, 1):
            status = "active" if row.is_active else "inactive"
            keys = row.key_count
            print(f"  {i}. {row.name} ({status}, {keys} key{'s' if keys != 1 else ''})")


def list_keys(args: argparse.Namespace) -> None:
    """List API keys for a tenant."""
    with get_sync_session() as session:
        tenant = session.execute(
            select(Tenant).where(Tenant.name == args.tenant)
        ).scalar_one_or_none()
        if tenant is None:
            print(f"Tenant not found: {args.tenant}", file=sys.stderr)
            sys.exit(1)

        keys = (
            session.execute(
                select(APIKey)
                .where(APIKey.tenant_id == tenant.id)
                .order_by(APIKey.created_at)
            )
            .scalars()
            .all()
        )

        if not keys:
            print(f'No keys for "{args.tenant}".')
            return

        print(f'Keys for "{args.tenant}":')
        for i, key in enumerate(keys, 1):
            status = "active" if key.is_active else "revoked"
            scopes = ",".join(key.scopes) if key.scopes else "none"
            print(f"  {i}. {key.key_prefix} [{key.label}] scopes={scopes} {status}")


def revoke_key(args: argparse.Namespace) -> None:
    """Revoke an API key by its prefix."""
    with get_sync_session() as session:
        key = session.execute(
            select(APIKey).where(APIKey.key_prefix == args.prefix)
        ).scalar_one_or_none()
        if key is None:
            print(f"Key not found: {args.prefix}", file=sys.stderr)
            sys.exit(1)

        if not key.is_active:
            print(f"Key already revoked: {args.prefix}", file=sys.stderr)
            sys.exit(1)

        key.is_active = False
        session.commit()
        print(f"Key revoked: {args.prefix}")


def deactivate_tenant(args: argparse.Namespace) -> None:
    """Deactivate a tenant (all keys become invalid)."""
    with get_sync_session() as session:
        tenant = session.execute(
            select(Tenant).where(Tenant.name == args.name)
        ).scalar_one_or_none()
        if tenant is None:
            print(f"Tenant not found: {args.name}", file=sys.stderr)
            sys.exit(1)

        if not tenant.is_active:
            print(f"Tenant already inactive: {args.name}", file=sys.stderr)
            sys.exit(1)

        tenant.is_active = False
        session.commit()
        print(f"Tenant deactivated: {args.name}")


def main() -> None:
    """Parse arguments and dispatch to command handler."""
    parser = argparse.ArgumentParser(description="Tenant management CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # create-tenant
    p = sub.add_parser("create-tenant", help="Create a new tenant")
    p.add_argument("--name", required=True, help="Tenant name")

    # create-key
    p = sub.add_parser("create-key", help="Generate API key for a tenant")
    p.add_argument("--tenant", required=True, help="Tenant name")
    p.add_argument("--scopes", required=True, help="Comma-separated: prep,check")
    p.add_argument("--label", default="default", help="Key label")
    p.add_argument("--rate-prep", type=int, default=60, help="Prep rate limit/min")
    p.add_argument("--rate-check", type=int, default=300, help="Check rate limit/min")

    # list-tenants
    sub.add_parser("list-tenants", help="List all tenants")

    # list-keys
    p = sub.add_parser("list-keys", help="List API keys for a tenant")
    p.add_argument("--tenant", required=True, help="Tenant name")

    # revoke-key
    p = sub.add_parser("revoke-key", help="Revoke an API key")
    p.add_argument("--prefix", required=True, help="Key prefix to revoke")

    # deactivate-tenant
    p = sub.add_parser("deactivate-tenant", help="Deactivate a tenant")
    p.add_argument("--name", required=True, help="Tenant name")

    args = parser.parse_args()
    commands: dict[str, Callable[[argparse.Namespace], None]] = {
        "create-tenant": create_tenant,
        "create-key": create_key,
        "list-tenants": list_tenants,
        "list-keys": list_keys,
        "revoke-key": revoke_key,
        "deactivate-tenant": deactivate_tenant,
    }
    commands[args.command](args)
```

> **Ключові рішення:**
> - **Sync session** — CLI запускається та завершується, `psycopg v3` нативно підтримує sync.
> - **`Callable` type hint** — `dict[str, Callable[[argparse.Namespace], None]]` для dispatch map замість `dict[str, object]`.
> - **Duplicate check** — `create_tenant` перевіряє чи tenant з таким ім'ям вже існує.
> - **`sys.exit(1)`** — для error cases (not found, already exists, already revoked).
> - **Без emoji** — plain text output замість emoji (ruff T20 compliance, портабельність).
> - **`_args` prefix** — `list_tenants(_args)` для unused parameter.

## Структура файлів

```
scripts/
└── manage_tenant.py      # CLI entrypoint

tests/unit/
└── test_manage_tenant.py  # 7 тестів, mocked DB session
```

## Тести

Файл: `tests/unit/test_manage_tenant.py` — **7 тестів**

1. `test_create_tenant` — створює tenant з правильним name, перевіряє duplicates
2. `test_create_key_outputs_full_key` — виводить повний ключ, prefix, scopes, label
3. `test_create_key_unknown_tenant` — неіснуючий tenant → `sys.exit(1)`
4. `test_revoke_key` — деактивує ключ по prefix, sets `is_active=False`
5. `test_list_tenants` — виводить список з key counts, singular/plural "key(s)"
6. `test_list_keys` — виводить ключі tenant з status (active/revoked)
7. `test_deactivate_tenant` — sets tenant `is_active=False`

Test fixtures:
- `mock_session` — `MagicMock` з `__enter__`/`__exit__` для context manager
- `_patch_session` — patches `scripts.manage_tenant.get_sync_session`, underscore prefix для side-effect fixture

## Definition of Done

- [x] CLI з 6 subcommands (create-tenant, create-key, list-tenants, list-keys, revoke-key, deactivate-tenant)
- [x] Повний ключ виводиться тільки при створенні
- [x] Duplicate check в `create_tenant`
- [x] Sync DB access для CLI (psycopg v3)
- [x] `Callable` type hint для command dispatch
- [x] 7 тестів зелені
- [x] `make check` зелений
