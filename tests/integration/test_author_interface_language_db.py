"""Integration: the author panel's language kept on the tenant (V1).

``GET`` / ``PUT /author/interface-language`` with the real committed session.
The tenant comes from a stub key context (``get_current_tenant`` overridden),
except in the no-key test, where the real API-key dependency answers. Two
tenants are seeded: A is the key's, B is the isolation probe.

Covered:
* GET is null until the author picks; PUT ``en`` stores and serves ``eng``.
* A language outside ``config/languages.yaml`` (or unknown) is a 422 and the
  stored one stays.
* No key → 401; a key without ``prep`` → 403, on both routes.
* Tenant A's key neither reads nor changes tenant B's language.
* An unchanged choice writes nothing: ``updated_at`` stays, no log event.
* The migration ``v1_author_interface_language`` down and up again.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, delete, inspect, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.config import get_settings
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import Tenant

pytestmark = pytest.mark.requires_db

_PATH = "/api/v1/author/interface-language"
_EVENT = "tenant_interface_language_updated"

REVISION = "v1_author_interface_language"
BELOW = "o1_open_enrollment"


@pytest.fixture()
async def tenants(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """Tenant A (the key's) and tenant B (the isolation probe)."""
    async with session_factory() as session:
        tenant_a = Tenant(name=f"v1-a-{uuid.uuid4().hex[:8]}")
        tenant_b = Tenant(name=f"v1-b-{uuid.uuid4().hex[:8]}")
        session.add_all([tenant_a, tenant_b])
        await session.commit()
        seed = {"a": tenant_a.id, "b": tenant_b.id}

    yield seed

    async with session_factory() as session:
        await session.execute(delete(Tenant).where(Tenant.id.in_(seed.values())))
        await session.commit()


def _context(tenant_id: uuid.UUID, scopes: list[str]) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        tenant_name="v1-test",
        scopes=scopes,
        plan_id="basic",
        key_prefix="cs_test",
    )


@contextmanager
def _overrides(
    session_factory: async_sessionmaker[AsyncSession],
    tenant: TenantContext | None,
) -> Iterator[None]:
    """Real committed session; the key's tenant stubbed unless ``None``."""

    async def _yield_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _yield_session
    if tenant is not None:
        app.dependency_overrides[get_current_tenant] = lambda: tenant
    try:
        yield
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
async def client(
    tenants: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient]:
    """Client with tenant A's ``prep`` key."""
    with _overrides(session_factory, _context(tenants["a"], ["prep"])):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


async def _stored(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> Tenant:
    async with session_factory() as session:
        tenant = await session.get(Tenant, tenant_id)
    assert tenant is not None
    return tenant


class TestReadAndWrite:
    async def test_null_until_picked_then_the_canonical_code(
        self,
        client: AsyncClient,
        tenants: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        first = await client.get(_PATH)
        assert first.status_code == 200, first.text
        assert first.json() == {"interface_language": None}

        with capture_logs() as logs:
            put = await client.put(_PATH, json={"language": "en"})
        assert put.status_code == 200, put.text
        assert put.json() == {"interface_language": "eng"}
        assert [e for e in logs if e["event"] == _EVENT] == [
            {
                "event": _EVENT,
                "log_level": "info",
                "tenant_id": str(tenants["a"]),
                "language": "eng",
            }
        ]

        again = await client.get(_PATH)
        assert again.status_code == 200, again.text
        assert again.json() == {"interface_language": "eng"}
        assert (await _stored(session_factory, tenants["a"])).interface_language == (
            "eng"
        )

    @pytest.mark.parametrize(
        "language",
        [pytest.param("xx", id="unknown"), pytest.param("kor", id="not-allowed")],
    )
    async def test_a_bad_language_is_422_and_the_stored_one_stays(
        self,
        client: AsyncClient,
        tenants: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        language: str,
    ) -> None:
        assert (await client.put(_PATH, json={"language": "ukr"})).status_code == 200

        put = await client.put(_PATH, json={"language": language})
        assert put.status_code == 422, put.text
        assert (await _stored(session_factory, tenants["a"])).interface_language == (
            "ukr"
        )

    async def test_null_is_not_a_language(self, client: AsyncClient) -> None:
        put = await client.put(_PATH, json={"language": None})
        assert put.status_code == 422, put.text

    async def test_an_unchanged_choice_writes_nothing(
        self,
        client: AsyncClient,
        tenants: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        assert (await client.put(_PATH, json={"language": "ukr"})).status_code == 200
        before = (await _stored(session_factory, tenants["a"])).updated_at

        with capture_logs() as logs:
            put = await client.put(_PATH, json={"language": "uk"})
        assert put.status_code == 200, put.text
        assert put.json() == {"interface_language": "ukr"}
        assert [e for e in logs if e["event"] == _EVENT] == []
        assert (await _stored(session_factory, tenants["a"])).updated_at == before


class TestTenantIsolation:
    async def test_a_key_reads_and_changes_only_its_own_tenant(
        self,
        client: AsyncClient,
        tenants: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            await session.execute(
                update(Tenant)
                .where(Tenant.id == tenants["b"])
                .values(interface_language="ukr")
            )
            await session.commit()

        read = await client.get(_PATH)
        assert read.json() == {"interface_language": None}

        put = await client.put(_PATH, json={"language": "eng"})
        assert put.status_code == 200, put.text
        assert (await _stored(session_factory, tenants["a"])).interface_language == (
            "eng"
        )
        assert (await _stored(session_factory, tenants["b"])).interface_language == (
            "ukr"
        )


class TestAccess:
    @pytest.mark.parametrize("method", ["GET", "PUT"])
    async def test_without_a_key_is_401(
        self,
        tenants: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        method: str,
    ) -> None:
        with _overrides(session_factory, None):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                response = await ac.request(method, _PATH, json={"language": "eng"})
        assert response.status_code == 401, response.text

    @pytest.mark.parametrize("method", ["GET", "PUT"])
    async def test_a_key_without_prep_is_403_and_writes_nothing(
        self,
        tenants: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        method: str,
    ) -> None:
        with _overrides(session_factory, _context(tenants["a"], ["check"])):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                response = await ac.request(method, _PATH, json={"language": "eng"})
        assert response.status_code == 403, response.text
        assert (await _stored(session_factory, tenants["a"])).interface_language is (
            None
        )


def test_one_head_and_the_revision_follows_open_enrollment() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    revision = script.get_revision(REVISION)
    assert len(heads) == 1
    assert revision is not None
    assert revision.down_revision == BELOW
    assert REVISION in {rev.revision for rev in script.walk_revisions("base", heads[0])}


def test_migration_down_and_up_keeps_existing_tenants() -> None:
    cfg = Config("alembic.ini")
    engine = create_engine(get_settings().database_url)
    tenant_id = uuid.uuid4()

    def has_column() -> bool:
        with engine.connect() as conn:
            columns = inspect(conn).get_columns("tenants")
        return "interface_language" in {c["name"] for c in columns}

    try:
        command.downgrade(cfg, BELOW)
        assert not has_column()
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO tenants (id, name, is_active) VALUES (:t, :n, true)"),
                {"t": tenant_id, "n": f"v1-mig-{tenant_id.hex[:8]}"},
            )

        command.upgrade(cfg, REVISION)
        assert has_column()
        with engine.connect() as conn:
            stored = conn.scalar(
                text("SELECT interface_language FROM tenants WHERE id = :t"),
                {"t": tenant_id},
            )
            comment = conn.scalar(
                text(
                    "SELECT col_description('tenants'::regclass, attnum) "
                    "FROM pg_attribute WHERE attrelid = 'tenants'::regclass "
                    "AND attname = 'interface_language'"
                )
            )
        assert stored is None
        assert comment == Tenant.__table__.c.interface_language.comment

        command.downgrade(cfg, BELOW)
        assert not has_column()
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
    with engine.connect() as conn:
        head = ScriptDirectory.from_config(cfg).get_current_head()
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == head
    assert has_column()
    engine.dispose()
