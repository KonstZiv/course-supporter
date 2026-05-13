"""Integration tests for Phase 2.1 C8 — Stage 1 wiring in confirm_upload.

Closes DD-1.2-4 (confirm_upload was Stage-1-less since Phase 1.2 KD14
§6.1 (a) ratify -- ext-only check; full libmagic / size / charset /
archive checks deferred to this commit).

Three baseline scenarios per PHASE.md §"Komit 8" acceptance:

* **Happy path** -- valid markdown bytes in S3, head_response size below
  policy cap; ``run_stage1`` passes; HTTP 201 with the new
  ``AuthoredDocument.raw_hash`` field populated.
* **Size reject** -- head_response.ContentLength exceeds the policy
  size cap for the resolved extension; HTTP 400 with
  ``SECURITY_REJECTED`` / ``SIZE_LIMIT`` body; no DB row created
  (verified via direct ``SELECT`` post-call).
* **Libmagic mismatch** -- the bytes claim to be a PDF (filename
  extension) but carry an MZ executable header; ``run_stage1`` raises
  ``SecurityRejectedError`` with a content-mismatch category; HTTP 400;
  no DB row.

Uses the established mock-S3 pattern (dependency overrides) from
``test_authored_upload_validation.py``: full FastAPI request pipeline
runs (auth + tenant + node ownership + dependency injection); the
underlying ``S3Client`` is replaced with an ``AsyncMock`` whose
``head_object`` / ``get_object`` returns are scripted per test. Hits
the real PostgreSQL for the DB verification side; the absent-row
checks confirm the rejection branch did not leak an
``AuthoredDocument`` insertion.

Test marks: ``requires-db`` per PHASE.md §3.4 — invokes the FastAPI
app's full request pipeline.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant, get_s3_client
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import AuthoredDocument


@pytest.fixture()
def stub_tenant(committed_seeds: dict[str, uuid.UUID]) -> TenantContext:
    """TenantContext pointing at the real committed_seeds tenant.

    Needed so the live ``_require_node_for_tenant`` check in
    ``confirm_upload`` resolves successfully against the real
    ``CourseNode`` seeded by ``committed_seeds``.
    """
    return TenantContext(
        tenant_id=committed_seeds["tenant_id"],
        tenant_name="test-tenant",
        scopes=["prep", "check"],
        plan_id="basic",
        key_prefix="cs_test",
    )


def _build_app_overrides(
    *,
    tenant: TenantContext,
    session_factory: async_sessionmaker[AsyncSession],
    s3_mock: AsyncMock,
    arq_mock: MagicMock,
) -> None:
    """Wire FastAPI dependency overrides for the test request lifecycle."""

    async def _override_session() -> Any:
        async with session_factory() as session:
            yield session

    async def _override_tenant() -> TenantContext:
        return tenant

    async def _override_s3() -> AsyncMock:
        return s3_mock

    async def _override_arq() -> MagicMock:
        return arq_mock

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_tenant] = _override_tenant
    app.dependency_overrides[get_s3_client] = _override_s3
    app.dependency_overrides[get_arq_redis] = _override_arq


@pytest.fixture()
def _cleanup_overrides() -> Any:
    yield
    app.dependency_overrides.clear()


def _confirm_upload_body(*, tenant_id: uuid.UUID, node_id: uuid.UUID) -> dict[str, Any]:
    safe_key = f"tenants/{tenant_id}/nodes/{node_id}/{uuid.uuid4()}/fixture.md"
    return {
        "key": safe_key,
        "source_type": "text",
        "material_role": "educational",
        "filename": "fixture.md",
    }


def _confirm_upload_body_pdf(
    *, tenant_id: uuid.UUID, node_id: uuid.UUID
) -> dict[str, Any]:
    safe_key = f"tenants/{tenant_id}/nodes/{node_id}/{uuid.uuid4()}/fixture.pdf"
    return {
        "key": safe_key,
        "source_type": "presentation",
        "material_role": "educational",
        "filename": "fixture.pdf",
    }


@pytest.mark.asyncio
@pytest.mark.requires_db
class TestConfirmUploadStage1:
    """C8 — Stage 1 dispatch under the presigned-confirm flow."""

    async def test_happy_path_persists_authored_document_with_raw_hash(
        self,
        committed_seeds: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        stub_tenant: TenantContext,
        _cleanup_overrides: Any,
    ) -> None:
        """Valid markdown bytes → Stage 1 passes → 201 + raw_hash persisted."""
        body = _confirm_upload_body(
            tenant_id=committed_seeds["tenant_id"],
            node_id=committed_seeds["course_node_id"],
        )
        valid_md = (
            b"# Confirm Upload Stage 1 Test\n\n"
            b"Body paragraph for libmagic to classify as text/plain or text/markdown."
        )

        s3 = AsyncMock()
        s3.head_object = AsyncMock(return_value={"ContentLength": len(valid_md)})
        s3.get_object = AsyncMock(return_value=valid_md)
        s3.get_object_url = MagicMock(
            return_value=f"http://localhost:9000/course-materials/{body['key']}"
        )

        arq = MagicMock()
        arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq-job-1"))

        _build_app_overrides(
            tenant=stub_tenant,
            session_factory=session_factory,
            s3_mock=s3,
            arq_mock=arq,
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/nodes/{committed_seeds['course_node_id']}/documents"
                "/confirm-upload",
                json=body,
            )

        assert response.status_code == 201, response.text
        s3.get_object.assert_awaited_once_with(body["key"])

        # Verify raw_hash persisted and matches sha256 of the bytes we fed in.
        import hashlib

        expected_hash = hashlib.sha256(valid_md).hexdigest()

        async with session_factory() as session:
            result = await session.execute(
                select(AuthoredDocument)
                .where(
                    AuthoredDocument.course_node_id == committed_seeds["course_node_id"]
                )
                .where(AuthoredDocument.source_url.like(f"%{body['key']}"))
            )
            doc = result.scalar_one_or_none()
        assert doc is not None, "AuthoredDocument did not persist"
        assert doc.raw_hash == expected_hash, (
            f"raw_hash mismatch: got {doc.raw_hash!r} expected {expected_hash!r}"
        )

    async def test_size_reject_no_db_row(
        self,
        committed_seeds: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        stub_tenant: TenantContext,
        _cleanup_overrides: Any,
    ) -> None:
        """head_response.ContentLength > policy cap → 400, no DB row."""
        body = _confirm_upload_body(
            tenant_id=committed_seeds["tenant_id"],
            node_id=committed_seeds["course_node_id"],
        )
        # AUTHORED_POLICY.max_file_size_bytes is the policy default; choose a
        # comically large content_length to guarantee rejection regardless of
        # the configured cap.
        oversize_length = 10 * 1024 * 1024 * 1024  # 10 GiB

        s3 = AsyncMock()
        s3.head_object = AsyncMock(return_value={"ContentLength": oversize_length})
        # get_object should never be reached -- size check runs first.
        s3.get_object = AsyncMock(
            side_effect=AssertionError("get_object should not be called")
        )
        s3.get_object_url = MagicMock(return_value="unused")

        _build_app_overrides(
            tenant=stub_tenant,
            session_factory=session_factory,
            s3_mock=s3,
            arq_mock=MagicMock(),
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/nodes/{committed_seeds['course_node_id']}/documents"
                "/confirm-upload",
                json=body,
            )

        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "SECURITY_REJECTED"
        assert detail["category"] == "size_limit"
        s3.get_object.assert_not_awaited()

        # No AuthoredDocument row created for this rejected key.
        async with session_factory() as session:
            result = await session.execute(
                select(AuthoredDocument).where(
                    AuthoredDocument.source_url.like(f"%{body['key']}")
                )
            )
            assert result.scalar_one_or_none() is None

    async def test_libmagic_mismatch_no_db_row(
        self,
        committed_seeds: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        stub_tenant: TenantContext,
        _cleanup_overrides: Any,
    ) -> None:
        """Bytes mismatch extension → Stage 1 rejects → 400, no DB row."""
        body = _confirm_upload_body_pdf(
            tenant_id=committed_seeds["tenant_id"],
            node_id=committed_seeds["course_node_id"],
        )
        # Win32 PE executable header masquerading as a .pdf upload. libmagic
        # detects it as ``application/x-dosexec`` (or similar), which
        # ``verify_extension_matches_content`` will reject against the .pdf
        # extension family.
        pe_header = (
            b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff" + b"\x00" * 256
        )

        s3 = AsyncMock()
        s3.head_object = AsyncMock(return_value={"ContentLength": len(pe_header)})
        s3.get_object = AsyncMock(return_value=pe_header)
        s3.get_object_url = MagicMock(return_value="unused")

        _build_app_overrides(
            tenant=stub_tenant,
            session_factory=session_factory,
            s3_mock=s3,
            arq_mock=MagicMock(),
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/nodes/{committed_seeds['course_node_id']}/documents"
                "/confirm-upload",
                json=body,
            )

        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "SECURITY_REJECTED"
        # Category is whatever Stage 1 raises for MIME / extension mismatch.
        # The category set is stable; assert it's in the rejection family
        # rather than a specific value to keep the test resilient to Stage 1
        # internal categorisation refinements.
        assert detail["category"] in {
            "forbidden_type",
            "content_mismatch",
            "mime_mismatch",
            "magic_mismatch",
        }, f"unexpected rejection category: {detail['category']!r}"
        s3.get_object.assert_awaited_once_with(body["key"])

        async with session_factory() as session:
            result = await session.execute(
                select(AuthoredDocument).where(
                    AuthoredDocument.source_url.like(f"%{body['key']}")
                )
            )
            assert result.scalar_one_or_none() is None
