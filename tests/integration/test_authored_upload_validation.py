"""Integration tests for Phase 1.2.3 — authored Stage 1 wiring + drift.

Phase 1.2 C3 replaced hand-rolled ``api/upload_validation.ALLOWED_EXTENSIONS``
with canonical ``security/run_stage1`` invocation against
``AUTHORED_POLICY``. This shifted the accepted-extension whitelist for
authored uploads, with 8 net behavioral changes (the "ratified whitelist
drift" — see ``phases/phase-1-2/POST-MERGE-NOTES.md``):

* 2 contraction (was accepted, now rejected): ``htm``, ``markdown``.
* 6 expansion (was rejected, now accepted): ``flac``, ``m4a``, ``mov``,
  ``mp3``, ``ogg``, ``wav``.

These tests parameterize across all 8 drift extensions plus 2 baseline
cases (``pdf`` always-accepted positive baseline; ``exe`` always-rejected
negative baseline) at the ``POST /api/v1/nodes/{id}/documents/upload-url``
endpoint. The presigned-URL endpoint runs the ext-only direct check
against ``AUTHORED_POLICY.allowed_extensions`` per Phase 1.2 §6.1 (a)
ratify; this test class verifies the wiring of that check against the
authored policy whitelist (file content is not available pre-presigned
upload).

The full Stage 1 invocation (libmagic / size / charset / archive
structure) lives in ``create_document`` (multipart path) — covered by
unit tests at ``tests/unit/test_api/test_authored_documents.py`` and the
canonical Stage 1 logic tests at ``tests/unit/security/test_stage1.py``
(Phase 0.6 sealed library).

Test marks: ``requires-db`` per PHASE.md §3.4 — invokes the FastAPI
app's full request pipeline (auth + tenant context + node ownership
check + dependency injection).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant, get_s3_client
from course_supporter.auth.context import TenantContext
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.database import get_session

_STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep", "check"],
    plan_id="basic",
    key_prefix="cs_test",
)

# Phase 1.2 C3 ratified whitelist drift — 8 extensions per
# POST-MERGE-NOTES "Ratified whitelist drift" section.
#
# (extension, expected_status, label)
#
# Status semantics:
# * ``rejected`` — endpoint returns 400 with KD14 ``SECURITY_REJECTED``
#   schema body.
# * ``accepted`` — endpoint returns 200 with ``PresignedUrlResponse``.
_DRIFT_CASES: list[tuple[str, str, str]] = [
    # 2 contraction — was accepted (hand-rolled), now rejected (policy):
    ("htm", "rejected", "drift-rejection-htm"),
    ("markdown", "rejected", "drift-rejection-markdown"),
    # 6 expansion — was rejected (hand-rolled), now accepted (policy):
    ("flac", "accepted", "drift-acceptance-flac"),
    ("m4a", "accepted", "drift-acceptance-m4a"),
    ("mov", "accepted", "drift-acceptance-mov"),
    ("mp3", "accepted", "drift-acceptance-mp3"),
    ("ogg", "accepted", "drift-acceptance-ogg"),
    ("wav", "accepted", "drift-acceptance-wav"),
]

# 2 baseline cases — no drift (behavior unchanged pre/post C3).
_BASELINE_CASES: list[tuple[str, str, str]] = [
    ("pdf", "accepted", "baseline-positive-pdf"),
    ("exe", "rejected", "baseline-negative-exe"),
]

_ALL_CASES: list[tuple[str, str, str]] = _DRIFT_CASES + _BASELINE_CASES


@pytest.fixture()
def stub_node_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def stub_node(stub_node_id: uuid.UUID) -> MagicMock:
    """Mock CourseNode owned by the stub tenant."""
    node = MagicMock()
    node.id = stub_node_id
    node.tenant_id = _STUB_TENANT.tenant_id
    return node


@pytest.fixture()
def mock_session(stub_node: MagicMock) -> AsyncMock:
    """Mock AsyncSession; CourseNodeRepository.get_by_id returns the stub node."""
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
def mock_s3() -> AsyncMock:
    s3 = AsyncMock()
    s3.generate_presigned_url = AsyncMock(
        return_value="https://example.com/presigned/upload"
    )
    return s3


@pytest.fixture()
def mock_arq() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def app_with_overrides(
    mock_session: AsyncMock,
    mock_s3: AsyncMock,
    mock_arq: MagicMock,
    stub_node: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Override FastAPI dependencies with mocks; clean up on teardown."""

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

    # Stub CourseNodeRepository.get_by_id to return the owned-by-tenant node.
    async def _stub_get_by_id(self: Any, node_id: uuid.UUID) -> MagicMock:
        return stub_node

    monkeypatch.setattr(
        CourseNodeRepository,
        "get_by_id",
        _stub_get_by_id,
    )

    yield app
    app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.requires_db
class TestAuthoredUploadDriftEnumeration:
    """Wiring correctness for Phase 1.2 C3 ratified whitelist drift.

    Parametrized across 8 drift extensions + 2 baseline cases per Phase
    1.2 pre-flight §6.5 ratify. Exercises ``POST /api/v1/nodes/{id}/
    documents/upload-url`` (presigned URL endpoint, ext-only direct
    check against ``AUTHORED_POLICY.allowed_extensions``).
    """

    @pytest.mark.parametrize(
        ("extension", "expected_status", "label"),
        _ALL_CASES,
        ids=[case[2] for case in _ALL_CASES],
    )
    async def test_presigned_url_validation(
        self,
        extension: str,
        expected_status: str,
        label: str,
        app_with_overrides: Any,
        stub_node_id: uuid.UUID,
    ) -> None:
        filename = f"submission.{extension}"
        body = {
            "filename": filename,
            "content_type": "application/octet-stream",
            "source_type": "presentation",  # endpoint accepts presentation/text/video
        }

        async with AsyncClient(
            transport=ASGITransport(app=app_with_overrides),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/v1/nodes/{stub_node_id}/documents/upload-url",
                json=body,
                headers={"X-API-Key": "stub-key-bypassed-via-dep-override"},
            )

        if expected_status == "accepted":
            assert response.status_code == 200, (
                f"[{label}] expected 200, got {response.status_code}: {response.text}"
            )
            payload = response.json()
            assert "upload_url" in payload, f"[{label}] missing upload_url"
            assert "key" in payload, f"[{label}] missing key"
        else:
            assert response.status_code == 400, (
                f"[{label}] expected 400, got {response.status_code}: {response.text}"
            )
            payload = response.json()
            # FastAPI wraps HTTPException(detail=...) as {"detail": ...}.
            # KD14 schema fields live inside the wrapped detail dict.
            assert "detail" in payload, f"[{label}] missing detail"
            kd14 = payload["detail"]
            assert kd14["code"] == "SECURITY_REJECTED", (
                f"[{label}] code mismatch: {kd14}"
            )
            assert kd14["category"] == "forbidden_type", (
                f"[{label}] category mismatch: {kd14}"
            )
            assert "details" in kd14, f"[{label}] missing details: {kd14}"
            assert extension in kd14["details"] or f".{extension}" in kd14["details"], (
                f"[{label}] details should mention extension {extension!r}: {kd14}"
            )
