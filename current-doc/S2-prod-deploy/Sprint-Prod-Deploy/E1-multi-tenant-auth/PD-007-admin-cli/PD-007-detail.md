# PD-007: Admin CLI для управління tenants — Detail

## Контекст

Потрібен інструмент для onboarding нових клієнтів та управління API ключами без UI. CLI з subcommands.

## CLI Interface

```bash
# Створити tenant
python -m scripts.manage_tenant create-tenant --name "Python Academy"

# Видати API key
python -m scripts.manage_tenant create-key \
    --tenant "Python Academy" \
    --scopes prep,check \
    --label production \
    --rate-prep 60 \
    --rate-check 300

# Output:
# ✅ API key created for "Python Academy":
#    Key:     cs_live_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4
#    Prefix:  cs_live_a1b2
#    Scopes:  prep, check
#    Label:   production
#
# ⚠️  Save this key now — it cannot be retrieved later!

# Список tenants
python -m scripts.manage_tenant list-tenants

# Output:
# Tenants:
#   1. Python Academy (active, 2 keys)
#   2. DevOps School (active, 1 key)

# Список ключів tenant
python -m scripts.manage_tenant list-keys --tenant "Python Academy"

# Output:
# Keys for "Python Academy":
#   1. cs_live_a1b2 [production] scopes=prep,check active
#   2. cs_live_x9y8 [staging]    scopes=prep       active

# Відкликати ключ
python -m scripts.manage_tenant revoke-key --prefix cs_live_a1b2

# Деактивувати tenant (всі ключі стають невалідними)
python -m scripts.manage_tenant deactivate-tenant --name "Python Academy"
```

## Реалізація

```python
# scripts/manage_tenant.py

"""CLI for tenant and API key management.

Usage:
    uv run python -m scripts.manage_tenant <command> [options]
"""

import argparse
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from course_supporter.auth.keys import generate_api_key
from course_supporter.config import settings
from course_supporter.storage.orm import APIKey, Tenant


def get_sync_session() -> Session:
    """Create sync session for CLI operations."""
    # Replace async driver with sync for CLI
    sync_url = settings.database_url.replace("+psycopg", "+psycopg")
    engine = create_engine(sync_url)
    return Session(engine)


def create_tenant(args: argparse.Namespace) -> None:
    with get_sync_session() as session:
        tenant = Tenant(name=args.name)
        session.add(tenant)
        session.commit()
        print(f"✅ Tenant created: {args.name} (id: {tenant.id})")


def create_key(args: argparse.Namespace) -> None:
    with get_sync_session() as session:
        tenant = session.execute(
            select(Tenant).where(Tenant.name == args.tenant)
        ).scalar_one_or_none()
        if tenant is None:
            print(f"❌ Tenant not found: {args.tenant}", file=sys.stderr)
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

        print(f'✅ API key created for "{args.tenant}":')
        print(f"   Key:     {full_key}")
        print(f"   Prefix:  {key_prefix}")
        print(f"   Scopes:  {', '.join(scopes)}")
        print(f"   Label:   {args.label}")
        print()
        print("⚠️  Save this key now — it cannot be retrieved later!")


def list_tenants(args: argparse.Namespace) -> None:
    ...


def list_keys(args: argparse.Namespace) -> None:
    ...


def revoke_key(args: argparse.Namespace) -> None:
    ...


def main() -> None:
    parser = argparse.ArgumentParser(description="Tenant management CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # create-tenant
    p = sub.add_parser("create-tenant")
    p.add_argument("--name", required=True)

    # create-key
    p = sub.add_parser("create-key")
    p.add_argument("--tenant", required=True)
    p.add_argument("--scopes", required=True, help="Comma-separated: prep,check")
    p.add_argument("--label", default="default")
    p.add_argument("--rate-prep", type=int, default=60)
    p.add_argument("--rate-check", type=int, default=300)

    # list-tenants
    sub.add_parser("list-tenants")

    # list-keys
    p = sub.add_parser("list-keys")
    p.add_argument("--tenant", required=True)

    # revoke-key
    p = sub.add_parser("revoke-key")
    p.add_argument("--prefix", required=True)

    # deactivate-tenant
    p = sub.add_parser("deactivate-tenant")
    p.add_argument("--name", required=True)

    args = parser.parse_args()
    commands = {
        "create-tenant": create_tenant,
        "create-key": create_key,
        "list-tenants": list_tenants,
        "list-keys": list_keys,
        "revoke-key": revoke_key,
        "deactivate-tenant": deactivate_tenant,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
```

## Тести

Файл: `tests/unit/test_manage_tenant.py`

Мокнута DB session, тестуємо логіку commands.

1. **test_create_tenant** — створює tenant з правильним name
2. **test_create_key_outputs_full_key** — виводить повний ключ
3. **test_create_key_unknown_tenant** — неіснуючий tenant → exit(1)
4. **test_revoke_key** — деактивує ключ по prefix
5. **test_list_tenants** — виводить список

Очікувана кількість тестів: **5**

## Definition of Done

- [ ] CLI з 6 subcommands
- [ ] Повний ключ виводиться тільки при створенні
- [ ] Sync DB access для CLI
- [ ] 5 тестів зелені
- [ ] `make check` зелений
- [ ] Документ оновлений відповідно до фінальної реалізації
