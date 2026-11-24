"""A stored review with a field this code does not know, on a real database.

The debt DD-SP-DL. A newer release wrote a field into a review; production was
rolled back to this code; the review is whole in the database, and three
readers must show it as it is:

* the portal's detail of the submission gives the remarks, not an empty review;
* the ``reviewed`` webhook sends the ``passed`` the review holds, not ``False``;
* the rule of the copy finds the review as a source: the same work again gets
  its review again, with no new check to pay for.

The first review is a real one, from the real body of the new path
(``test_attempt_dedup_db`` walks it); then its stored row is rewritten as the
newer release would have written it — unknown fields at the root, in a remark,
in ``verdict.rule`` — and the readers are asked. Run with ``--run-db --run-redis``.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_student
from course_supporter.auth.context import StudentContext
from course_supporter.homework.attempt_dedup import DEDUP_RECEIPT_KEY
from course_supporter.homework.webhook import build_reviewed_payload
from course_supporter.models.review_structure import ReviewStructureV1
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import HomeworkSubmission, Student
from tests._helpers.stored_review import MARKER, UNKNOWN, with_unknown
from tests._helpers.stored_review import Path as Place
from tests.integration.test_attempt_dedup_db import (
    _CP1251_CRLF,
    _first_review,
    _NoModelRouter,
    _providers_watched,
    _register_rows,
    _walk,
)
from tests.integration.test_attempt_ledger_db import _drop_world, _make_world, _row
from tests.integration.test_text_review_e2e_db import _ListSource, _PortDouble

pytestmark = pytest.mark.requires_db

_IGNORED = "stored_unknown_fields_ignored"


@pytest.fixture()
async def task_world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, Any]]:
    world = await _make_world(session_factory, task_type="task")
    yield world
    await _drop_world(session_factory, world)


def _places(review: dict[str, Any]) -> list[Place]:
    """Where a newer release would have put a field: root, a remark, the rule."""
    places: list[Place] = [(), ("verdict",)]
    if review["verdict"].get("rule") is not None:
        places.append(("verdict", "rule"))
    for section in ("new_remarks", "open", "fixed", "broken"):
        if review[section]:
            places.append((section, 0))
    return places


async def _reviewed_by_a_newer_release(
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    tmp_path: Path,
) -> tuple[HomeworkSubmission, dict[str, Any], dict[str, Any]]:
    """A real first review, passed, its row then rewritten with unknown fields.

    The review the real body wrote is a failed one; only its verdict is made a
    pass here (and its rule agrees), because a read that fails shows ``False``
    and only a pass tells the two apart. Its remarks are the real ones.

    Returns the submission as the first review left it, the review as stored
    before the unknown fields, and the review as a newer release would have
    stored it.
    """
    first = await _first_review(session_factory, world, tmp_path)
    assert first.review_result is not None
    clean = copy.deepcopy(first.review_result)
    clean["verdict"] = {
        "passed": True,
        "why": "The work is accepted.",
        "rule": {"basis": "no_author_musts_passed", "score": 90, "threshold": 80},
    }
    ReviewStructureV1.model_validate(clean)  # a review the models write as it is
    assert clean["new_remarks"], "the debt bites on a review with remarks to show"
    noisy = with_unknown(clean, *_places(clean))
    async with session_factory() as session:
        await session.execute(
            update(HomeworkSubmission)
            .where(HomeworkSubmission.id == first.id)
            .values(review_result=noisy)
        )
        await session.commit()
    return first, clean, noisy


@pytest.fixture()
def signed_in(task_world: dict[str, Any], session_factory: Any) -> Iterator[None]:
    student = StudentContext(
        student_id=task_world["student_id"],
        tenant_id=task_world["tenant_id"],
        login="student",
        display_name=None,
    )

    async def _session() -> Any:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_student] = lambda: student
    yield
    app.dependency_overrides.clear()


class TestThePortalDetail:
    @pytest.mark.usefixtures("signed_in")
    async def test_the_remarks_are_given_not_an_empty_review(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        first, clean, _ = await _reviewed_by_a_newer_release(
            session_factory, task_world, tmp_path
        )
        expected = ReviewStructureV1.model_validate(clean)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            with capture_logs() as logs:
                response = await client.get(f"/api/v1/portal/submissions/{first.id}")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["structure"] is not None
        assert body["structure"]["verdict"]["passed"] is True
        remarks = [
            *body["structure"]["new_remarks"],
            *body["structure"]["open"],
            *body["structure"]["fixed"],
            *body["structure"]["broken"],
        ]
        assert remarks, "the review the row holds has no remark to show"
        assert body["structure"] == expected.model_dump(mode="json")
        assert body["verdict"]["passed"] is True
        assert UNKNOWN not in response.text
        assert MARKER not in response.text
        # The row is read, said once for each read of it, never as invalid.
        events = {entry["event"] for entry in logs}
        assert _IGNORED in events
        assert "review_structure_invalid" not in events
        assert MARKER not in repr(logs)


class TestTheWebhook:
    async def test_passed_is_the_one_the_review_holds_not_false(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        first, clean, _ = await _reviewed_by_a_newer_release(
            session_factory, task_world, tmp_path
        )

        async with session_factory() as session:
            submission = await session.get(HomeworkSubmission, first.id)
            student = await session.get(Student, task_world["student_id"])
            assert submission is not None
            assert student is not None
            payload = build_reviewed_payload(submission, student)

        assert payload.review.passed is clean["verdict"]["passed"] is True
        assert payload.structure == ReviewStructureV1.model_validate(clean)
        assert UNKNOWN not in payload.model_dump_json()


class TestTheRuleOfTheCopy:
    async def test_the_same_work_after_such_a_source_gets_a_copy_not_a_new_check(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        first, clean, _ = await _reviewed_by_a_newer_release(
            session_factory, task_world, tmp_path
        )
        register_before = await _register_rows(session_factory)
        router = _NoModelRouter()
        port = _PortDouble()

        with _providers_watched() as providers, capture_logs() as logs:
            second_id = await _walk(
                session_factory,
                task_world,
                _CP1251_CRLF,
                tmp_path,
                router=router,
                source=_ListSource(task_world["in_force"]),
                port=port,
            )

        second = await _row(session_factory, second_id)
        # ── A copy: the receipt names the attempt it repeats ──
        assert second.attempt_receipt is not None
        assert second.attempt_receipt[DEDUP_RECEIPT_KEY] == 1
        assert second.previous_submission_id == first.id
        # ── Nothing new was checked, nothing asked, nothing paid ──
        assert router.calls == []
        assert not any(mock.await_count for mock in providers)
        assert await _register_rows(session_factory) == register_before
        assert (port.reserved, port.accounted, port.released) == ([], [], [])
        # ── The review of the source, without the fields it did not know ──
        assert second.review_result == {**clean, "unchanged_from_attempt": 1}
        assert second.status == "delivered"
        # ── Said once, with the source named, and never as unreadable ──
        [skipped] = [entry for entry in logs if entry["event"] == _IGNORED]
        assert skipped["submission_id"] == str(first.id)
        assert MARKER not in repr(logs)
        assert "attempt_dedup_review_unreadable" not in {
            entry["event"] for entry in logs
        }
