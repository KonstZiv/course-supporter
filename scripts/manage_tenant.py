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


if __name__ == "__main__":
    main()
