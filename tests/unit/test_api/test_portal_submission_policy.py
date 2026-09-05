"""Unit tests for the portal submission-policy lookup (step Д, DD-SP-V).

The route exists so the form stops carrying its own copies of the size cap
and the format list. A test that re-typed those numbers here would be the
same defect one repository over, so nothing below states a value: every
assertion either reads the server's own source, or drives the server's own
door with the value the route served and checks the door agrees.

The route is authenticated, so ``get_current_student`` is overridden per
client — the policy itself is built for real.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_student
from course_supporter.auth.context import StudentContext
from course_supporter.homework.submission_core import (
    ALLOWED_HOMEWORK_EXTENSIONS,
    MAX_HOMEWORK_SIZE,
    PROJECT_SUBMISSION_MAX_UPLOAD_BYTES,
    validate_homework_file,
)
from course_supporter.models.source import AssignmentType
from course_supporter.security.stage1 import archive_kind_for_filename

_URL = "/api/v1/portal/submission-policy"

STUB_STUDENT = StudentContext(
    student_id=uuid.uuid4(),
    tenant_id=uuid.uuid4(),
    login="alice",
    display_name="Alice",
)


class _SizedUpload:
    """The two attributes ``validate_homework_file`` reads off an upload.

    A real ``UploadFile`` needs a file object and buffers bytes; the door
    only ever looks at ``filename`` and ``size``, and building 100 MB to
    prove a cap would be a test that measures the machine.
    """

    def __init__(self, filename: str, size: int) -> None:
        self.filename = filename
        self.size = size


def _door(filename: str, size: int, max_bytes: int) -> None:
    """Run the real door with a served cap; raises on refusal."""
    upload = _SizedUpload(filename, size)
    validate_homework_file(
        upload,  # type: ignore[arg-type]
        max_upload_bytes=max_bytes,
    )


@pytest.fixture()
def student_client() -> AsyncClient:
    app.dependency_overrides[get_current_student] = lambda: STUB_STUDENT
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides.clear()


@pytest.fixture()
async def policies(student_client: AsyncClient) -> dict[str, dict[str, object]]:
    async with student_client as ac:
        resp = await ac.get(_URL)
    assert resp.status_code == 200
    body: dict[str, dict[str, dict[str, object]]] = resp.json()
    return body["policies"]


class TestEveryKindHasARow:
    async def test_keys_are_exactly_the_assignment_types(
        self, policies: dict[str, dict[str, object]]
    ) -> None:
        """All four, not the two the server branches on.

        The portal renders whatever ``task_type`` the document carries. A
        table keyed on ``task``/``project`` alone would leave ``test`` and
        ``short_task`` without a row and push a "not project → task" rule
        onto the client — server logic on the far side of the repository
        boundary, which is the defect DD-SP-V is about.
        """
        assert set(policies) == {kind.value for kind in AssignmentType}


class TestValuesMatchTheServer:
    @pytest.mark.parametrize("kind", list(AssignmentType))
    async def test_cap_is_the_one_the_route_resolves(
        self, policies: dict[str, dict[str, object]], kind: AssignmentType
    ) -> None:
        # The same test the submission route makes on task_type.
        expected = (
            PROJECT_SUBMISSION_MAX_UPLOAD_BYTES
            if kind is AssignmentType.PROJECT
            else MAX_HOMEWORK_SIZE
        )
        assert policies[kind.value]["max_bytes"] == expected

    @pytest.mark.parametrize("kind", list(AssignmentType))
    async def test_accept_is_the_door_allowlist(
        self, policies: dict[str, dict[str, object]], kind: AssignmentType
    ) -> None:
        # Task-agnostic on the server, so identical on every row — and
        # already dot-prefixed, which is the form ``accept=`` needs.
        assert policies[kind.value]["accept"] == sorted(ALLOWED_HOMEWORK_EXTENSIONS)

    @pytest.mark.parametrize("kind", list(AssignmentType))
    async def test_archive_only_matches_the_preflight_rule(
        self, policies: dict[str, dict[str, object]], kind: AssignmentType
    ) -> None:
        # ``project_preflight`` refuses a loose file with ARCHIVE_ONLY, and
        # only for a project.
        assert policies[kind.value]["archive_only"] is (kind is AssignmentType.PROJECT)


class TestTheDoorAgreesWithWhatWasServed:
    """Drive the real door with the served values, per kind.

    This is the assertion that would survive someone editing the route to
    return a plausible-looking number: a cap the door does not enforce, or
    an extension it refuses, fails here.
    """

    @pytest.mark.parametrize("kind", list(AssignmentType))
    async def test_a_file_at_the_served_cap_passes(
        self, policies: dict[str, dict[str, object]], kind: AssignmentType
    ) -> None:
        cap = int(policies[kind.value]["max_bytes"])  # type: ignore[call-overload]
        _door("solution.zip", cap, cap)

    @pytest.mark.parametrize("kind", list(AssignmentType))
    async def test_one_byte_over_the_served_cap_is_refused(
        self, policies: dict[str, dict[str, object]], kind: AssignmentType
    ) -> None:
        cap = int(policies[kind.value]["max_bytes"])  # type: ignore[call-overload]
        with pytest.raises(HTTPException) as excinfo:
            _door("solution.zip", cap + 1, cap)
        assert excinfo.value.status_code == 422
        assert excinfo.value.detail["code"] == "size_limit"

    @pytest.mark.parametrize("kind", list(AssignmentType))
    async def test_every_served_extension_passes_the_door(
        self, policies: dict[str, dict[str, object]], kind: AssignmentType
    ) -> None:
        cap = int(policies[kind.value]["max_bytes"])  # type: ignore[call-overload]
        for ext in policies[kind.value]["accept"]:  # type: ignore[union-attr]
            _door(f"work{ext}", 1, cap)

    @pytest.mark.parametrize("kind", list(AssignmentType))
    async def test_an_unserved_extension_is_refused(
        self, policies: dict[str, dict[str, object]], kind: AssignmentType
    ) -> None:
        cap = int(policies[kind.value]["max_bytes"])  # type: ignore[call-overload]
        assert ".exe" not in policies[kind.value]["accept"]  # type: ignore[operator]
        with pytest.raises(HTTPException) as excinfo:
            _door("work.exe", 1, cap)
        assert excinfo.value.status_code == 422
        assert excinfo.value.detail["code"] == "forbidden_type"

    async def test_archive_only_names_kinds_the_archive_test_would_refuse(
        self, policies: dict[str, dict[str, object]]
    ) -> None:
        """``archive_only`` is about ``archive_kind_for_filename``.

        Where the flag is true, ``project_preflight`` runs that test and
        refuses anything it does not recognise. Checked here so the flag
        cannot drift from the function it stands for.
        """
        assert archive_kind_for_filename("solution.zip") is not None
        assert archive_kind_for_filename("solution.md") is None
        for kind in AssignmentType:
            assert policies[kind.value]["archive_only"] is (
                kind is AssignmentType.PROJECT
            )


class TestDoorStaysShut:
    async def test_route_needs_a_session(self) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get(_URL)
        assert resp.status_code == 401
