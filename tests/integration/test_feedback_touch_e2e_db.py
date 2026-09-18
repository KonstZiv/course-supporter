"""Acceptance of task 05, walked end to end (mentor-rebuild task 05).

The criterion this file exists for is a THROUGH one — "a touch from the portal
and a touch from the caller's channel land on one review as one record, and the
counters agree" — and a through criterion is not proved by a pile of unit tests,
each of which can be green while the joints between them are broken
(``vision-rules#25``).

So nothing here is called directly. Both touches go through their HTTP routes,
with the scope each one really requires: the portal session is a real bearer
token obtained by a real login, the channel touch carries a key context of scope
CHECK and nothing else, and the counters are read with a key context of scope
PREP and nothing else. The database is live. No model is called — and that is
measured, not assumed, by counting the register before and after.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Student,
    StudentCredential,
    StudentCredentialToken,
    StudentEnrollment,
    StudentFeedback,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_PASSWORD = "correct horse 10"
_LOGIN = "e2e-student"
_EXTERNAL_ID = "channel-e2e-student"


@pytest.fixture()
async def seeded(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """Tenant → course → task → reviewed submission, committed for real.

    The submission is seeded with a review already written: producing one would
    mean calling a model, and this test is about what happens AFTER a review
    exists.
    """
    async with session_factory() as session:
        tenant = Tenant(name=f"e2e-feedback-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()

        root = make_root_course_node(
            tenant_id=tenant.id, title="Feedback E2E Course", order=0
        )
        session.add(root)
        await session.flush()

        doc = AuthoredDocument(
            course_node_id=root.id,
            course_root_id=root.id,
            source_type="text",
            source_url="https://example.com/homework.md",
            filename="homework.md",
            order=1,
            task_type="task",
        )
        session.add(doc)
        await session.flush()

        student = Student(tenant_id=tenant.id, external_id=_EXTERNAL_ID)
        session.add(student)
        await session.flush()

        session.add(StudentEnrollment(student_id=student.id, course_node_id=root.id))

        submission = HomeworkSubmission(
            tenant_id=tenant.id,
            student_id=student.id,
            course_node_id=root.id,
            node_id=root.id,
            authored_document_id=doc.id,
            file_url="s3://bucket/solution.py",
            file_type="text/plain",
            original_filename="solution.py",
            delivery_mode="in_app",
            status="completed",
            review_markdown="## Good work\nOne remark about naming.",
            score=76,
        )
        session.add(submission)
        await session.flush()
        await session.commit()

        ids = {
            "tenant_id": tenant.id,
            "course_node_id": root.id,
            "authored_document_id": doc.id,
            "student_id": student.id,
            "submission_id": submission.id,
        }

    yield ids

    async with session_factory() as session:
        await session.execute(
            StudentFeedback.__table__.delete().where(
                StudentFeedback.tenant_id == ids["tenant_id"]
            )
        )
        credential_ids = (
            (
                await session.execute(
                    select(StudentCredential.id).where(
                        StudentCredential.student_id == ids["student_id"]
                    )
                )
            )
            .scalars()
            .all()
        )
        if credential_ids:
            await session.execute(
                StudentCredentialToken.__table__.delete().where(
                    StudentCredentialToken.credential_id.in_(credential_ids)
                )
            )
        await session.execute(
            StudentCredential.__table__.delete().where(
                StudentCredential.student_id == ids["student_id"]
            )
        )
        await session.execute(
            StudentEnrollment.__table__.delete().where(
                StudentEnrollment.student_id == ids["student_id"]
            )
        )
        await session.execute(
            HomeworkSubmission.__table__.delete().where(
                HomeworkSubmission.tenant_id == ids["tenant_id"]
            )
        )
        await session.execute(
            Student.__table__.delete().where(Student.tenant_id == ids["tenant_id"])
        )
        await session.execute(
            AuthoredDocument.__table__.delete().where(
                AuthoredDocument.course_node_id == ids["course_node_id"]
            )
        )
        await session.execute(
            CourseNode.__table__.delete().where(CourseNode.id == ids["course_node_id"])
        )
        await session.execute(
            Tenant.__table__.delete().where(Tenant.id == ids["tenant_id"])
        )
        await session.commit()


def _key(tenant_id: uuid.UUID, *scopes: str) -> TenantContext:
    """A key context carrying exactly the scopes named — no more."""
    return TenantContext(
        tenant_id=tenant_id,
        tenant_name="e2e",
        scopes=list(scopes),
        plan_id="basic",
        key_prefix="cs_e2e",
    )


@pytest.fixture()
async def client(
    seeded: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[AsyncClient, Callable[[TenantContext], None]]]:
    """A live client plus a switch for the key context of the next request.

    ``get_current_student`` is NOT overridden: the portal half of this test runs
    the real bearer flow. The key context IS overridden, but its scopes are set
    per request, so every scope check below is a real one against a key that
    carries only what that route requires.
    """

    async def _yield_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _yield_session
    app.dependency_overrides[get_current_tenant] = lambda: _key(
        seeded["tenant_id"], "prep"
    )

    def use_key(ctx: TenantContext) -> None:
        app.dependency_overrides[get_current_tenant] = lambda: ctx

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, use_key

    app.dependency_overrides.clear()


async def _register_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(select(func.count()).select_from(ExternalServiceCall))
            or 0
        )


async def _feedback_rows(
    session_factory: async_sessionmaker[AsyncSession],
    submission_id: uuid.UUID,
) -> list[StudentFeedback]:
    async with session_factory() as session:
        result = await session.execute(
            select(StudentFeedback).where(StudentFeedback.target_id == submission_id)
        )
        return list(result.scalars().all())


class TestAcceptance:
    async def test_two_entries_leave_one_record_and_the_counters_agree(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        seeded: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The whole criterion, in one walk.

        A student answers "it helped" from the portal, then answers "it did not"
        from the caller's channel about the SAME review; one record remains,
        carrying the second answer, and the author's counters say zero helped
        and one not-helped. Not one model call is made.
        """
        ac, use_key = client
        tenant_id = seeded["tenant_id"]
        submission_id = seeded["submission_id"]
        calls_before = await _register_rows(session_factory)

        # --- The student gets a portal login (author side, scope PREP) ---
        use_key(_key(tenant_id, "prep"))
        provision = await ac.post(
            "/api/v1/students",
            json={
                "mode": "existing",
                "student_id": str(seeded["student_id"]),
                "login": _LOGIN,
                "password": _PASSWORD,
            },
        )
        assert provision.status_code == 201, provision.text

        login = await ac.post(
            "/api/v1/portal/login",
            json={
                "tenant_id": str(tenant_id),
                "login": _LOGIN,
                "password": _PASSWORD,
            },
        )
        assert login.status_code == 200, login.text
        bearer = {"Authorization": f"Bearer {login.json()['access_token']}"}

        # --- 1. The portal door: the student's own session ---
        portal_touch = await ac.post(
            f"/api/v1/portal/submissions/{submission_id}/feedback",
            json={"kind": "touch", "value": "helped"},
            headers=bearer,
        )
        assert portal_touch.status_code == 200, portal_touch.text
        assert portal_touch.json()["value"] == "helped"

        # The probe that makes the count below mean something: there IS a row.
        after_first = await _feedback_rows(session_factory, submission_id)
        assert len(after_first) == 1, "the portal touch wrote nothing"
        first_written_at = after_first[0].updated_at

        # --- 2. The channel door: a key of scope CHECK and nothing else ---
        use_key(_key(tenant_id, "check"))
        channel_touch = await ac.post(
            f"/api/v1/feedback/submissions/{submission_id}",
            json={
                "kind": "touch",
                "value": "not_helped",
                "student_external_id": _EXTERNAL_ID,
            },
        )
        assert channel_touch.status_code == 200, channel_touch.text
        assert channel_touch.json()["value"] == "not_helped"

        # --- 3. One record, carrying the second answer ---
        rows = await _feedback_rows(session_factory, submission_id)
        assert len(rows) == 1, (
            f"the second door added a row instead of replacing: {len(rows)} rows"
        )
        row = rows[0]
        assert row.value == "not_helped"
        assert row.student_id == seeded["student_id"]
        assert row.tenant_id == tenant_id
        assert row.updated_at > first_written_at, (
            "the replacement did not move the time"
        )

        # --- 4. The student sees their current answer on the next read ---
        detail = await ac.get(
            f"/api/v1/portal/submissions/{submission_id}", headers=bearer
        )
        assert detail.status_code == 200
        assert detail.json()["own_feedback"]["value"] == "not_helped"

        # --- 5. The author reads the counters with a key of scope PREP ---
        use_key(_key(tenant_id, "prep"))
        task_counters = await ac.get(
            f"/api/v1/feedback/counters/task/{seeded['authored_document_id']}"
        )
        assert task_counters.status_code == 200, task_counters.text
        assert task_counters.json()["helped"] == 0
        assert task_counters.json()["not_helped"] == 1

        course_counters = await ac.get(
            f"/api/v1/feedback/counters/course/{seeded['course_node_id']}"
        )
        assert course_counters.status_code == 200, course_counters.text
        by_task: list[dict[str, Any]] = course_counters.json()["by_task"]
        assert by_task, "the course breakdown found no task — nothing was measured"
        assert [
            (t["authored_document_id"], t["helped"], t["not_helped"]) for t in by_task
        ] == [(str(seeded["authored_document_id"]), 0, 1)]

        # --- 6. Not one model call was made along the way ---
        assert await _register_rows(session_factory) == calls_before

    async def test_each_door_refuses_the_scope_it_is_not(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        seeded: dict[str, uuid.UUID],
    ) -> None:
        """The scope checks above are real, and this is what makes them real."""
        ac, use_key = client

        use_key(_key(seeded["tenant_id"], "prep"))
        touch = await ac.post(
            f"/api/v1/feedback/submissions/{seeded['submission_id']}",
            json={
                "kind": "touch",
                "value": "helped",
                "student_external_id": _EXTERNAL_ID,
            },
        )
        assert touch.status_code == 403

        use_key(_key(seeded["tenant_id"], "check"))
        counters = await ac.get(
            f"/api/v1/feedback/counters/task/{seeded['authored_document_id']}"
        )
        assert counters.status_code == 403
