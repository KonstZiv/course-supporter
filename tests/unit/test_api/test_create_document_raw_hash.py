"""Unit tests for Phase 2.1 C8 — raw_hash population in create_document.

Closes DD-1.1-1 (``AuthoredDocument.raw_hash`` never populated in
production). Strategy A per KD-2.1-E: SHA-256 over the upload bytes is
computed at the route handler (the same bytes already loaded into
memory for Stage 1), then passed through to
``AuthoredDocumentRepository.create(raw_hash=...)``.

These tests pin three contracts:

* **Multipart with file** -- ``raw_hash`` equals
  ``hashlib.sha256(upload_bytes).hexdigest()`` and is forwarded to the
  repository ``create`` call.
* **URL-only path (no file)** -- ``raw_hash`` is ``None`` (no bytes
  available at the upload entry; the ingestion worker may populate it
  later out-of-scope for C8).
* **The hash is forwarded as a keyword argument** -- positional
  forwarding would break the existing call sites; pin the contract
  to keyword form so future signature additions don't silently
  re-order the raw_hash slot.

The tests run the full FastAPI request pipeline with the heavy
dependencies (S3, ARQ, session, tenant, node ownership check) replaced
by ``AsyncMock`` / ``MagicMock`` overrides; the repository ``create``
method is monkeypatched to capture call kwargs so the test asserts on
the wire signature into the storage layer.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant, get_s3_client
from course_supporter.auth.context import TenantContext
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.database import get_session

_STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="raw-hash-test-tenant",
    scopes=["prep", "check"],
    plan_id="basic",
    key_prefix="cs_test",
)


@pytest.fixture()
def stub_node_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def stub_node(stub_node_id: uuid.UUID) -> MagicMock:
    node = MagicMock()
    node.id = stub_node_id
    node.tenant_id = _STUB_TENANT.tenant_id
    return node


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
def mock_s3() -> AsyncMock:
    s3 = AsyncMock()
    s3.upload_smart = AsyncMock(
        return_value=("http://localhost:9000/course-materials/test-key", 100)
    )
    return s3


@pytest.fixture()
def mock_arq() -> MagicMock:
    arq = MagicMock()
    arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq-job-1"))
    return arq


@pytest.fixture()
def app_with_overrides(
    mock_session: AsyncMock,
    mock_s3: AsyncMock,
    mock_arq: MagicMock,
    stub_node: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Override FastAPI deps + spy on repository create() call kwargs."""

    async def _override_session() -> Any:
        yield mock_session

    async def _override_tenant() -> TenantContext:
        return _STUB_TENANT

    async def _override_s3() -> AsyncMock:
        return mock_s3

    async def _override_arq() -> MagicMock:
        return mock_arq

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_tenant] = _override_tenant
    app.dependency_overrides[get_s3_client] = _override_s3
    app.dependency_overrides[get_arq_redis] = _override_arq

    async def _stub_get_by_id(self: Any, node_id: uuid.UUID) -> MagicMock:
        return stub_node

    monkeypatch.setattr(CourseNodeRepository, "get_by_id", _stub_get_by_id)

    # Spy on AuthoredDocumentRepository.create(): capture kwargs and return
    # a deterministic mock document so the route hits the response shape.
    captured: dict[str, Any] = {}

    fixed_now = datetime.now(UTC)

    async def _spy_create(self: Any, **kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        doc = MagicMock()
        doc.id = uuid.uuid4()
        doc.course_node_id = stub_node.id
        doc.source_type = kwargs.get("source_type", "text")
        doc.source_url = kwargs.get("source_url", "http://example.com")
        doc.filename = kwargs.get("filename")
        doc.material_role = kwargs.get("material_role", "educational")
        doc.task_type = kwargs.get("task_type")
        doc.language = kwargs.get("language")
        doc.raw_hash = kwargs.get("raw_hash")
        doc.state = "raw"
        doc.created_at = fixed_now
        doc.updated_at = fixed_now
        doc.job_id = None
        doc.warnings = []
        return doc

    monkeypatch.setattr(AuthoredDocumentRepository, "create", _spy_create)

    # Stub enqueue_ingestion at the route module's import binding so the
    # route doesn't try to enqueue a real job. Patching the source module
    # (course_supporter.enqueue) does NOT replace the bound import in the
    # route's namespace -- must patch the route's own reference.
    async def _stub_enqueue(*args: Any, **kwargs: Any) -> MagicMock:
        job = MagicMock()
        job.id = uuid.uuid4()
        return job

    monkeypatch.setattr(
        "course_supporter.api.routes.documents.enqueue_ingestion",
        _stub_enqueue,
    )

    yield {"app": app, "captured": captured}
    app.dependency_overrides.clear()


@pytest.mark.asyncio
class TestCreateDocumentRawHash:
    """C8 — raw_hash compute discipline in the multipart path."""

    async def test_file_upload_computes_sha256_and_forwards_to_repo(
        self,
        app_with_overrides: dict[str, Any],
        stub_node_id: uuid.UUID,
    ) -> None:
        captured = app_with_overrides["captured"]
        upload_bytes = (
            b"# Markdown header\n\nBody paragraph that libmagic will detect as text."
        )
        expected_hash = hashlib.sha256(upload_bytes).hexdigest()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/nodes/{stub_node_id}/documents",
                data={"source_type": "text"},
                files={
                    "file": ("fixture.md", io.BytesIO(upload_bytes), "text/markdown")
                },
            )

        assert response.status_code == 201, response.text
        assert "raw_hash" in captured, (
            "AuthoredDocumentRepository.create was not called with raw_hash kwarg"
        )
        assert captured["raw_hash"] == expected_hash, (
            f"raw_hash mismatch: {captured['raw_hash']!r} vs {expected_hash!r}"
        )

    async def test_url_only_path_passes_none_for_raw_hash(
        self,
        app_with_overrides: dict[str, Any],
        stub_node_id: uuid.UUID,
    ) -> None:
        captured = app_with_overrides["captured"]

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/nodes/{stub_node_id}/documents",
                data={
                    "source_type": "web",
                    "source_url": "https://example.com/article",
                },
            )

        assert response.status_code == 201, response.text
        assert captured.get("raw_hash") is None, (
            f"URL-only path should pass raw_hash=None, got {captured.get('raw_hash')!r}"
        )

    async def test_raw_hash_passed_as_keyword_not_positional(
        self,
        app_with_overrides: dict[str, Any],
        stub_node_id: uuid.UUID,
    ) -> None:
        """Pins the wire contract to keyword form.

        The spy captures only kwargs; if the route called ``create`` with
        positional args, ``captured`` would miss ``raw_hash`` even when
        the bytes hash was computed. This protects against silent argument
        re-ordering in future signature changes.
        """
        captured = app_with_overrides["captured"]
        upload_bytes = b"another fixture body for the keyword-arg pin"

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/nodes/{stub_node_id}/documents",
                data={"source_type": "text"},
                files={"file": ("pin.md", io.BytesIO(upload_bytes), "text/markdown")},
            )

        assert response.status_code == 201
        assert "raw_hash" in captured  # keyword form, not lost to positional
