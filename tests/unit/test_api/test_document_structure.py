"""Tests for ``GET /documents/{id}/structure`` (step Г2 §1.3).

The post-hoc half of R5 visibility: after a code material is processed, the
author can see which of their files never reached the model and why.

Four things are pinned here, and they are the four ways this surface could
go quietly wrong: the stored composite reason is split so the interface has
a dictionary key; the collapse count survives so a folder of five files does
not read as one; a document with no structure answers the SAME 404 as one
that does not exist; and a foreign tenant gets that answer too.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.ingestion.code_structure import CodeStructureReason
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.database import get_session
from course_supporter.storage.document_summary_repository import (
    DocumentSummaryRepository,
)

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep"],
    plan_id="basic",
    key_prefix="cs_test",
)

# Real rows, in the shape ``_build_structure`` persists them: a bare token, a
# composite ``token: detail``, and a collapsed directory carrying a count.
_ENTRIES: list[dict[str, Any]] = [
    {"path": "src/app.py", "size": 100, "cls": "included", "reason": None},
    {
        "path": "package-lock.json",
        "size": 24,
        "cls": "description_only",
        "reason": "lockfile: package-lock.json",
        "role": "structure_only",
    },
    {
        "path": "__MACOSX/",
        "size": 200,
        "cls": "excluded",
        "reason": "denylist_dir",
        "role": None,
        "entries": 5,
    },
    {
        "path": "notes.txt",
        "size": 90,
        "cls": "excluded",
        "reason": "charset_violation",
        "role": None,
        "entries": 1,
    },
]


def _mock_document(tenant_ok: bool = True) -> tuple[MagicMock, MagicMock]:
    document = MagicMock()
    document.id = uuid.uuid4()
    document.course_node_id = uuid.uuid4()
    node = MagicMock()
    node.id = document.course_node_id
    node.tenant_id = STUB_TENANT.tenant_id if tenant_ok else uuid.uuid4()
    return document, node


def _mock_summary(structure: dict[str, Any] | None) -> MagicMock:
    summary = MagicMock()
    summary.structure = structure
    return summary


@pytest.fixture()
async def client() -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: AsyncMock()
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


async def _get(
    client: AsyncClient,
    *,
    structure: dict[str, Any] | None,
    summary_exists: bool = True,
    tenant_ok: bool = True,
) -> Any:
    document, node = _mock_document(tenant_ok=tenant_ok)
    with (
        patch.object(AuthoredDocumentRepository, "get_by_id", return_value=document),
        patch.object(CourseNodeRepository, "get_by_id", return_value=node),
        patch.object(
            DocumentSummaryRepository,
            "get_by_authored_document_id",
            return_value=_mock_summary(structure) if summary_exists else None,
        ),
    ):
        return await client.get(f"/api/v1/documents/{document.id}/structure")


class TestStructureListing:
    async def test_splits_the_composite_reason(self, client: AsyncClient) -> None:
        """The stored string is ``token: detail``; the interface needs the token.

        Without the split the UI dictionary would be keyed on
        ``"lockfile: package-lock.json"`` and never match — and the detail,
        which is the half the author recognises, would be unreachable.
        """
        resp = await _get(client, structure={"entries": _ENTRIES})
        assert resp.status_code == 200
        [row] = resp.json()["description_only"]
        assert row["path"] == "package-lock.json"
        assert row["reason"] == "lockfile"
        assert row["detail"] == "package-lock.json"

    async def test_bare_reason_has_no_detail(self, client: AsyncClient) -> None:
        resp = await _get(client, structure={"entries": _ENTRIES})
        rows = {r["path"]: r for r in resp.json()["excluded"]}
        assert rows["notes.txt"]["reason"] == "charset_violation"
        assert rows["notes.txt"]["detail"] is None

    async def test_collapse_count_survives(self, client: AsyncClient) -> None:
        """``__MACOSX/`` stands for five files and must not read as one."""
        resp = await _get(client, structure={"entries": _ENTRIES})
        rows = {r["path"]: r for r in resp.json()["excluded"]}
        assert rows["__MACOSX/"]["entries"] == 5
        assert rows["notes.txt"]["entries"] == 1

    async def test_included_files_are_not_listed(self, client: AsyncClient) -> None:
        """This surface answers "what did I lose", not "what is in my archive"."""
        resp = await _get(client, structure={"entries": _ENTRIES})
        body = resp.json()
        assert set(body) == {"excluded", "description_only"}
        paths = {r["path"] for r in body["excluded"] + body["description_only"]}
        assert "src/app.py" not in paths

    async def test_read_in_full_is_two_empty_lists_not_a_404(
        self, client: AsyncClient
    ) -> None:
        """A code material with nothing skipped HAS a surface — it is just empty.

        The distinction the 404 carries is "no such surface", so a project
        that was read whole must answer 200.
        """
        resp = await _get(client, structure={"entries": _ENTRIES[:1]})
        assert resp.status_code == 200
        assert resp.json() == {"excluded": [], "description_only": []}

    async def test_malformed_rows_cost_their_own_line_only(
        self, client: AsyncClient
    ) -> None:
        """Free-form JSONB with older writers behind it — same rule as not_opened."""
        resp = await _get(
            client,
            structure={
                "entries": [
                    "a string",
                    None,
                    {"cls": "excluded"},  # no path/size/reason
                    {"path": "x", "size": "big", "cls": "excluded", "reason": "y"},
                    {
                        "path": "ok.png",
                        "size": 10,
                        "cls": "excluded",
                        "reason": "non_code_type",
                    },
                ]
            },
        )
        assert resp.status_code == 200
        rows = resp.json()["excluded"]
        assert [r["path"] for r in rows] == ["ok.png"]
        # No ``entries`` key on that row → null, not a fabricated 1.
        assert rows[0]["entries"] is None


class TestStructureNotFound:
    """Absence of a surface is not an empty listing — and never distinguishable.

    All three misses answer the same 404 with the same body: an unknown id,
    a document belonging to another tenant, and a document with no structure
    (non-code, or code still being processed). Comparing the bodies is the
    point — a different phrase for "exists but not yours" would be an
    enumeration oracle (impl-rules#9).
    """

    async def test_no_summary_yet(self, client: AsyncClient) -> None:
        resp = await _get(client, structure=None, summary_exists=False)
        assert resp.status_code == 404

    async def test_summary_without_structure_is_a_non_code_material(
        self, client: AsyncClient
    ) -> None:
        # The column is NULL for every non-code source_type, so it is the
        # discriminator — no separate source_type branch is needed.
        resp = await _get(client, structure=None)
        assert resp.status_code == 404

    async def test_foreign_tenant(self, client: AsyncClient) -> None:
        resp = await _get(client, structure={"entries": _ENTRIES}, tenant_ok=False)
        assert resp.status_code == 404

    async def test_all_three_answers_are_byte_identical(
        self, client: AsyncClient
    ) -> None:
        missing = await _get(client, structure=None, summary_exists=False)
        foreign = await _get(client, structure={"entries": _ENTRIES}, tenant_ok=False)
        non_code = await _get(client, structure=None)
        assert missing.json() == foreign.json() == non_code.json()
        assert missing.json() == {"detail": "Document not found"}


class TestReasonVocabularyIsLocked:
    """Every token the writer can emit must be one the interface can phrase.

    The route deliberately passes an unknown token through rather than
    dropping the row (hiding a file the author could not read is the worse
    failure), so nothing at runtime enforces the vocabulary. This does: a
    new ``CodeStructureReason`` member arrives here before it arrives on a
    screen with no phrase for it.
    """

    def test_every_token_survives_the_round_trip(self) -> None:
        from course_supporter.ingestion.code_structure import (
            split_structure_reason,
            structure_reason,
        )

        for member in CodeStructureReason:
            assert split_structure_reason(structure_reason(member)) == (
                member.value,
                None,
            )
            assert split_structure_reason(structure_reason(member, "node_modules")) == (
                member.value,
                "node_modules",
            )

    def test_a_detail_carrying_its_own_separator_survives(self) -> None:
        from course_supporter.ingestion.code_structure import split_structure_reason

        assert split_structure_reason("vendored_dir: a: b") == ("vendored_dir", "a: b")
