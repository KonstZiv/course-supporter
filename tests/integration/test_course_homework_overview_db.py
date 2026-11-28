"""The author's overview of a course's homework, through its two routes (M-10, 27.11).

``GET /nodes/{course_id}/homework/summary`` and ``.../homework/students`` on a
live database, with the key context of a tenant that carries scope PREP.

One world, two tenants. The course ("Python Basics") has four tasks in a tree
whose order is told apart from creation order, four students whose attempts the
numbers below are counted from by hand, and the people the boundary must leave
out: a service student, a soft-deleted student, another tenant's course.

The numbers of the task "Sort a list" (``T1``), from the seed:

=====  ========================================  =====  ====  ==========
who    attempts (score, passed)                  best   pass  last verdicts
=====  ========================================  =====  ====  ==========
Ann    1 (40, no), 2 (90, yes), 3 (copy of 2)    90     yes   a2: all met
Bob    1 (60, no), 2 (65, no)                    65     no    c1 c3 c4 missed
Cyd    1 (80, yes)                               80     yes   c2 c3 missed
Dan    1 (30, no)                                30     no    c1 c4 missed
=====  ========================================  =====  ====  ==========

4 students, 7 attempts, pass share 2/4, mean best score 265/4 = 66.25, the
first passed attempt is 2 and 1 (median 1.5, mean 1.5). By the LAST attempt
``c1`` and ``c4`` are missed by 2 of 4, ``c2`` by 1 of 4 and ``c3`` by 2 of 4 —
but ``c3`` is a "may". Counting every attempt gives other numbers entirely
(``c1`` is missed in six verdicts), which is what the mutations of the task
turn the code into.

Requires ``docker compose up -d`` (or a local PostgreSQL); run with ``--run-db``.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.api.routes._portal_shared import curated_verdict
from course_supporter.auth.context import TenantContext
from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.homework.criteria_form import (
    Criterion,
    criteria_to_document,
)
from course_supporter.models import review_structure
from course_supporter.models.review_schema import REVIEW_SCHEMA_VERSION
from course_supporter.models.review_structure import (
    PassRule,
    ReviewStructureV1,
    Verdict,
)
from course_supporter.storage.course_homework_repository import (
    IN_FLIGHT_STATUSES,
    TASK_TYPES,
)
from course_supporter.storage.database import get_session
from course_supporter.storage.homework_repository import ATTEMPT_STATUSES
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkStatus,
    HomeworkSubmission,
    Student,
    StudentCredential,
    StudentEnrollment,
    SubmissionCriterionVerdict,
    TaskCriteriaOverride,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)

# Planted where a text of the work, of a review or of a quote lives: none of
# them may reach either response.
_MARKER = "TEXT-MARKER-3f9a"

COURSE_NOT_FOUND = "Course not found."

SUMMARY_KEYS = {
    "course_id",
    "course_title",
    "include_test_students",
    "excluded_test_students",
    "tasks",
}
TASK_KEYS = {
    "task_id",
    "title",
    "task_type",
    "node_id",
    "node_title",
    "students_submitted",
    "submissions",
    "pass_share",
    "average_score",
    "attempts_to_pass",
    "hardest_criteria",
}
CRITERION_KEYS = {
    "criterion_id",
    "text",
    "weight",
    "not_met_students",
    "judged_students",
    "share",
}
TABLE_KEYS = {
    "course_id",
    "course_title",
    "include_test_students",
    "excluded_test_students",
    "tasks",
    "students",
    "total",
    "limit",
    "offset",
}
COLUMN_KEYS = {"task_id", "title", "task_type", "node_id", "node_title"}
ROW_KEYS = {"student_id", "display_name", "login", "cells"}
CELL_KEYS = {"state", "best_score", "attempts", "last_submitted_at"}


def _criterion(cid: str, text: str, weight: str) -> Criterion:
    return Criterion(
        id=cid,
        text=text,
        evidence=f"evidence of {cid}",
        weight=WeightCategory(weight),
        check_method="model_verdict",
        soft_descent=False,
        concepts=(),
        mandatory_points=(),
        must_by_author=weight == "must",
    )


_LIST_T1 = (
    _criterion("c1", "Handles an empty list", "must"),
    _criterion("c2", "Explains the idea", "should"),
    _criterion("c3", "Uses type hints", "may"),
    _criterion("c4", "Covers the edge cases", "should"),
)
_LIST_T2 = (
    _criterion("c1", "Uses a loop", "must"),
    _criterion("c2", "Names the variables well", "should"),
)
_LIST_T3 = (_criterion("c1", "Has a README", "must"),)


def _review(*, passed: bool, score: int) -> dict[str, Any]:
    return ReviewStructureV1(
        schema_version=REVIEW_SCHEMA_VERSION,
        language="ukr",
        verdict=Verdict(
            passed=passed,
            why=_MARKER,
            rule=PassRule(
                basis="author_musts_met" if passed else "author_musts_unmet",
                score=score,
                threshold=70,
            ),
        ),
    ).model_dump(mode="json")


def _test_review(*, passed: bool, score: int) -> dict[str, Any]:
    return ReviewStructureV1(
        schema_version=REVIEW_SCHEMA_VERSION,
        language="ukr",
        verdict=Verdict(passed=passed, why=_MARKER),
        test=review_structure.TestSection(
            score=score,
            questions=[
                review_structure.TestQuestionResult(
                    number="1",
                    correct=passed,
                    correct_answer=None
                    if passed
                    else [review_structure.TestOption(label="B", text="")],
                )
            ],
        ),
    ).model_dump(mode="json")


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, Any]]:
    async with session_factory() as s:
        tenant = Tenant(name=f"m10-a-{uuid.uuid4().hex[:6]}")
        other = Tenant(name=f"m10-b-{uuid.uuid4().hex[:6]}")
        s.add_all([tenant, other])
        await s.flush()

        course = make_root_course_node(
            tenant_id=tenant.id, title="Python Basics", order=0
        )
        foreign = make_root_course_node(tenant_id=other.id, title="Foreign", order=0)
        s.add_all([course, foreign])
        await s.flush()

        def node(title: str, order: int, **kw: Any) -> CourseNode:
            return CourseNode(
                tenant_id=tenant.id,
                parent_id=course.id,
                title=title,
                order=order,
                **kw,
            )

        # Created in the order the tree must NOT show: module 2 first.
        module2 = node("Module 2", 1)
        module1 = node("Module 1", 0)
        gone_node = node("Dropped module", 2, deleted_at=_T0)
        s.add_all([module2, module1, gone_node])
        await s.flush()

        def doc(where: CourseNode, root: CourseNode, **kw: Any) -> AuthoredDocument:
            return AuthoredDocument(
                course_node_id=where.id,
                course_root_id=root.id,
                source_type="text",
                source_url=f"https://example.com/{uuid.uuid4().hex[:6]}",
                **kw,
            )

        t3 = doc(
            module2,
            course,
            title="Final project",
            task_type="project",
            content_hash="h3",
        )
        t2 = doc(
            module1, course, title="Loops", task_type="short_task", content_hash="h2"
        )
        quiz = doc(module1, course, title="Quiz", task_type="test", order=1)
        t1 = doc(
            course,
            course,
            title=None,
            filename="sort.md",
            task_type="task",
            content_hash="h1",
        )
        material = doc(course, course, title="Reading")  # no task_type
        dropped_task = doc(course, course, title="Dropped", task_type="task")
        dropped_task.deleted_at = _T0
        on_dropped_node = doc(gone_node, course, title="On dropped", task_type="task")
        foreign_task = doc(foreign, foreign, title="Foreign task", task_type="task")
        s.add_all(
            [t3, t2, quiz, t1, material, dropped_task, on_dropped_node, foreign_task]
        )
        await s.flush()

        def student(name: str | None, **kw: Any) -> Student:
            return Student(
                tenant_id=tenant.id,
                external_id=f"{name or 'nameless'}-{uuid.uuid4().hex[:4]}",
                display_name=name,
                **kw,
            )

        ann, bob, cyd, dan = (student(n) for n in ("Ann", "Bob", "Cyd", "Dan"))
        eva = student("Eva")  # submitted without enrolling
        fay = student("Fay")  # enrolled, never submitted
        nameless = student(None)
        tester = student("Test Student", is_test_student=True)
        gone = student("Gone", deleted_at=_T0)
        eve = Student(tenant_id=other.id, external_id="eve", display_name="Eve")
        s.add_all([ann, bob, cyd, dan, eva, fay, nameless, tester, gone, eve])
        await s.flush()
        s.add_all(
            [
                StudentCredential(
                    student_id=ann.id,
                    tenant_id=tenant.id,
                    login="ann-login",
                    password_hash="x",
                ),
                *(
                    StudentEnrollment(student_id=p.id, course_node_id=course.id)
                    for p in (ann, bob, cyd, dan, fay, nameless, tester, gone)
                ),
            ]
        )

        def override(
            on: AuthoredDocument, items: tuple[Criterion, ...], **kw: Any
        ) -> TaskCriteriaOverride:
            return TaskCriteriaOverride(
                authored_document_id=on.id,
                source_content_hash=on.content_hash,
                source_task_type=on.task_type,
                criteria=criteria_to_document(items),
                **kw,
            )

        l1 = override(t1, _LIST_T1)
        l2 = override(t2, _LIST_T2)
        l3_old = override(t3, _LIST_T3, deleted_at=_T0)
        l3 = override(t3, _LIST_T3)
        s.add_all([l1, l2, l3_old, l3])
        await s.flush()

        counter = 0

        async def submit(
            who: Student,
            on: AuthoredDocument,
            *,
            root: CourseNode = course,
            **fields: Any,
        ) -> HomeworkSubmission:
            nonlocal counter
            counter += 1
            fields.setdefault("tenant_id", who.tenant_id)
            row = HomeworkSubmission(
                student_id=who.id,
                course_node_id=root.id,
                node_id=on.course_node_id,
                authored_document_id=on.id,
                file_url=f"s3://bucket/{_MARKER}/work.py",
                file_type="text/plain",
                original_filename=f"{_MARKER}.py",
                review_markdown=f"# {_MARKER}",
                student_note=_MARKER,
                error_message=_MARKER,
                delivery_mode="in_app",
                created_at=_T0 + timedelta(hours=counter),
                **fields,
            )
            s.add(row)
            await s.flush()
            return row

        async def attempt(
            who: Student,
            on: AuthoredDocument,
            score: int,
            *,
            passed: bool,
            status: str = "completed",
            review: dict[str, Any] | None = None,
            **fields: Any,
        ) -> HomeworkSubmission:
            return await submit(
                who,
                on,
                status=status,
                score=score,
                review_result=review or _review(passed=passed, score=score),
                **fields,
            )

        def verdicts(
            sub: HomeworkSubmission,
            list_row: TaskCriteriaOverride,
            met: dict[str, bool],
            weights: dict[str, str],
        ) -> None:
            s.add_all(
                SubmissionCriterionVerdict(
                    tenant_id=sub.tenant_id,
                    submission_id=sub.id,
                    criteria_layer=CriteriaLayer.AUTHOR.value,
                    criteria_source_id=list_row.id,
                    item_id=cid,
                    item_kind=VerdictItemKind.CRITERION.value,
                    weight=weights[cid],
                    verdict=(VerdictValue.MET if ok else VerdictValue.NOT_MET).value,
                    model_verdict=(
                        VerdictValue.MET if ok else VerdictValue.NOT_MET
                    ).value,
                    quote=_MARKER if ok else None,
                    quote_file="work.py" if ok else None,
                    quote_line_start=3 if ok else None,
                    quote_line_end=3 if ok else None,
                    missing=None if ok else _MARKER,
                    quote_not_found=False,
                )
                for cid, ok in met.items()
            )

        w1 = {"c1": "must", "c2": "should", "c3": "may", "c4": "should"}

        # ── T1 "Sort a list": the table in the module's docstring ──
        a1 = await attempt(ann, t1, 40, passed=False, attempt_no=1)
        a2 = await attempt(ann, t1, 90, passed=True, attempt_no=2)
        await attempt(
            ann,
            t1,
            90,
            passed=True,
            status="delivered",
            attempt_no=3,
            attempt_receipt={"attempt_no": 3, "dedup_of": 2},
        )
        b1 = await attempt(bob, t1, 60, passed=False, attempt_no=1)
        b2 = await attempt(bob, t1, 65, passed=False, attempt_no=2)
        cy1 = await attempt(cyd, t1, 80, passed=True, attempt_no=1)
        d1 = await attempt(dan, t1, 30, passed=False, attempt_no=1)
        # A refused work is no attempt, and a failed run neither.
        await submit(dan, t1, status="rejected")
        await submit(cyd, t1, status="failed")
        # The service student and the deleted one: failed attempts, so that
        # counting either of them would move the numbers.
        t_sub = await attempt(tester, t1, 10, passed=False, attempt_no=1)
        await attempt(gone, t1, 10, passed=False, attempt_no=1)
        # Another tenant's row on this tenant's course: the tenant is the key's.
        await attempt(ann, t1, 10, passed=False, tenant_id=other.id, attempt_no=9)
        verdicts(a1, l1, dict(c1=False, c2=False, c3=False, c4=False), w1)
        verdicts(a2, l1, dict(c1=True, c2=True, c3=True, c4=True), w1)
        # a3 is a repeated review of a2: it has no rows of its own.
        verdicts(b1, l1, dict(c1=False, c2=False, c3=False, c4=True), w1)
        verdicts(b2, l1, dict(c1=False, c2=True, c3=False, c4=False), w1)
        verdicts(cy1, l1, dict(c1=True, c2=False, c3=False, c4=True), w1)
        verdicts(d1, l1, dict(c1=False, c2=True, c3=True, c4=False), w1)
        verdicts(t_sub, l1, dict(c1=False, c2=False, c3=False, c4=False), w1)

        # ── T2 "Loops": Ann passes, Bob does not ──
        w2 = {"c1": "must", "c2": "should"}
        l2a = await attempt(ann, t2, 100, passed=True, attempt_no=1)
        l2b = await attempt(bob, t2, 50, passed=False, attempt_no=1)
        verdicts(l2a, l2, dict(c1=True, c2=True), w2)
        verdicts(l2b, l2, dict(c1=False, c2=True), w2)
        # Ann submits again: in the work, and the cell stays "passed".
        await submit(ann, t2, status="received")
        # Dan's is held for funds: in the work.
        await submit(dan, t2, status="awaiting_funds")

        # ── The quiz: Ann passes, Cyd fails, Bob's is being checked ──
        await attempt(
            ann,
            quiz,
            100,
            passed=True,
            review=_test_review(passed=True, score=100),
        )
        await attempt(
            cyd,
            quiz,
            40,
            passed=False,
            review=_test_review(passed=False, score=40),
        )
        await submit(bob, quiz, status="reviewing")

        # ── T3 "Final project": judged against an EARLIER list, now replaced ──
        w3 = {"c1": "must"}
        p1 = await attempt(ann, t3, 85, passed=True, attempt_no=1)
        p2 = await attempt(eva, t3, 20, passed=False, attempt_no=1)
        verdicts(p1, l3_old, dict(c1=False), w3)
        verdicts(p2, l3_old, dict(c1=False), w3)
        # A refusal alone makes no cell of "submitted".
        await submit(cyd, t3, status="rejected")

        # Another tenant's student on another tenant's course.
        await attempt(eve, foreign_task, 70, passed=True, root=foreign)
        await s.commit()
        ids = {
            "tenant": tenant.id,
            "other": other.id,
            "course": course.id,
            "foreign": foreign.id,
            "module1": module1.id,
            "module2": module2.id,
            "t1": t1.id,
            "t2": t2.id,
            "t3": t3.id,
            "quiz": quiz.id,
            "ann": ann.id,
            "bob": bob.id,
            "cyd": cyd.id,
            "dan": dan.id,
            "eva": eva.id,
            "fay": fay.id,
            "nameless": nameless.id,
            "tester": tester.id,
            "gone": gone.id,
        }

    yield ids

    async with session_factory() as s:
        tenants = [ids["tenant"], ids["other"]]
        subs = select(HomeworkSubmission.id).where(
            HomeworkSubmission.tenant_id.in_(tenants)
        )
        await s.execute(
            delete(SubmissionCriterionVerdict).where(
                SubmissionCriterionVerdict.submission_id.in_(subs)
            )
        )
        await s.execute(
            delete(HomeworkSubmission).where(HomeworkSubmission.tenant_id.in_(tenants))
        )
        students = select(Student.id).where(Student.tenant_id.in_(tenants))
        await s.execute(
            delete(StudentEnrollment).where(StudentEnrollment.student_id.in_(students))
        )
        await s.execute(
            delete(StudentCredential).where(StudentCredential.student_id.in_(students))
        )
        await s.execute(delete(Student).where(Student.tenant_id.in_(tenants)))
        roots = select(CourseNode.id).where(CourseNode.tenant_id.in_(tenants))
        docs = select(AuthoredDocument.id).where(
            AuthoredDocument.course_root_id.in_(roots)
        )
        await s.execute(
            delete(TaskCriteriaOverride).where(
                TaskCriteriaOverride.authored_document_id.in_(docs)
            )
        )
        await s.execute(delete(AuthoredDocument).where(AuthoredDocument.id.in_(docs)))
        # Children first: the parent link is a foreign key.
        await s.execute(
            delete(CourseNode).where(
                CourseNode.tenant_id.in_(tenants), CourseNode.parent_id.is_not(None)
            )
        )
        await s.execute(delete(CourseNode).where(CourseNode.tenant_id.in_(tenants)))
        await s.execute(delete(Tenant).where(Tenant.id.in_(tenants)))
        await s.commit()


def _key(tenant_id: uuid.UUID, *scopes: str) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        tenant_name="m10",
        scopes=list(scopes),
        plan_id="basic",
        key_prefix="cs_m10",
    )


@pytest.fixture()
async def client(
    world: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[AsyncClient, Callable[[TenantContext], None]]]:
    async def _yield_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _yield_session
    app.dependency_overrides[get_current_tenant] = lambda: _key(world["tenant"], "prep")

    def use_key(ctx: TenantContext) -> None:
        app.dependency_overrides[get_current_tenant] = lambda: ctx

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac, use_key
    app.dependency_overrides.clear()


Client = tuple[AsyncClient, Callable[[TenantContext], None]]


def _summary(course: uuid.UUID, include_test: bool = False) -> str:
    return (
        f"/api/v1/nodes/{course}/homework/summary"
        f"?include_test_students={str(include_test).lower()}"
    )


def _table(course: uuid.UUID, query: str = "") -> str:
    return f"/api/v1/nodes/{course}/homework/students{query}"


async def _tasks(ac: AsyncClient, world: dict[str, Any], **kw: bool) -> dict[str, Any]:
    body: dict[str, Any] = (await ac.get(_summary(world["course"], **kw))).json()
    by_id: dict[str, Any] = {t["task_id"]: t for t in body["tasks"]}
    return by_id


# ── The boundary ────────────────────────────────────────────────────────────


class TestBoundary:
    async def test_another_tenants_course_is_the_one_404(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        unknown = uuid.uuid4()
        for path in (
            _summary(world["foreign"]),
            _table(world["foreign"]),
            _summary(unknown),
            _table(unknown),
            _summary(world["module1"]),  # a child node is no course
            _table(world["module1"]),
        ):
            response = await ac.get(path)
            assert response.status_code == 404, path
            assert response.json() == {"detail": COURSE_NOT_FOUND}

    async def test_a_deleted_course_is_the_same_404(
        self,
        client: Client,
        world: dict[str, Any],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ac, _ = client
        async with session_factory() as s:
            course = await s.get(CourseNode, world["course"])
            assert course is not None
            course.deleted_at = _T0
            await s.commit()
        for path in (_summary(world["course"]), _table(world["course"])):
            response = await ac.get(path)
            assert response.status_code == 404
            assert response.json() == {"detail": COURSE_NOT_FOUND}

    async def test_a_key_without_scope_prep_is_403_as_in_the_neighbours(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, use_key = client
        use_key(_key(world["tenant"], "check"))
        neighbour = await ac.get("/api/v1/students")
        for path in (_summary(world["course"]), _table(world["course"])):
            assert (await ac.get(path)).status_code == neighbour.status_code == 403

    async def test_the_other_tenants_key_cannot_read_this_course(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, use_key = client
        use_key(_key(world["other"], "prep"))
        for path in (_summary(world["course"]), _table(world["course"])):
            assert (await ac.get(path)).status_code == 404

    async def test_the_in_flight_statuses_and_the_attempts_cover_every_status(
        self,
    ) -> None:
        ended_without_review = {"rejected", "mismatch", "failed"}
        everything = {s.value for s in HomeworkStatus}
        assert not ATTEMPT_STATUSES & IN_FLIGHT_STATUSES
        assert everything == (
            ATTEMPT_STATUSES | IN_FLIGHT_STATUSES | ended_without_review
        )
        assert set(TASK_TYPES) == {"test", "short_task", "task", "project"}


# ── The summary ─────────────────────────────────────────────────────────────


class TestSummary:
    async def test_the_tasks_are_the_courses_in_the_courses_order(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        body = (await ac.get(_summary(world["course"]))).json()
        assert set(body) == SUMMARY_KEYS
        assert body["course_id"] == str(world["course"])
        assert body["course_title"] == "Python Basics"
        # The root's own tasks, then Module 1 (order 0) with its tasks by their
        # order, then Module 2 (order 1) — not the order of creation. The
        # material, the deleted task, the task on a deleted node and the other
        # tenant's task are no columns.
        assert [t["task_id"] for t in body["tasks"]] == [
            str(world["t1"]),
            str(world["t2"]),
            str(world["quiz"]),
            str(world["t3"]),
        ]
        assert [
            (t["title"], t["task_type"], t["node_title"]) for t in body["tasks"]
        ] == [
            ("sort.md", "task", "Python Basics"),
            ("Loops", "short_task", "Module 1"),
            ("Quiz", "test", "Module 1"),
            ("Final project", "project", "Module 2"),
        ]
        assert all(set(t) == TASK_KEYS for t in body["tasks"])

    async def test_the_numbers_of_the_first_task_are_counted_by_hand(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        t1 = (await _tasks(ac, world))[str(world["t1"])]
        assert t1["students_submitted"] == 4
        assert t1["submissions"] == 7  # Ann 3, Bob 2, Cyd 1, Dan 1
        assert t1["pass_share"] == 0.5  # Ann and Cyd
        assert t1["average_score"] == 66.25  # (90 + 65 + 80 + 30) / 4
        assert t1["attempts_to_pass"] == {"median": 1.5, "mean": 1.5}  # 2 and 1

    async def test_the_numbers_of_the_other_tasks(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        tasks = await _tasks(ac, world)
        loops = tasks[str(world["t2"])]
        assert (loops["students_submitted"], loops["submissions"]) == (2, 2)
        assert loops["pass_share"] == 0.5
        assert loops["average_score"] == 75.0
        assert loops["attempts_to_pass"] == {"median": 1.0, "mean": 1.0}
        quiz = tasks[str(world["quiz"])]
        # Bob's is still being checked: no attempt yet.
        assert (quiz["students_submitted"], quiz["submissions"]) == (2, 2)
        assert quiz["pass_share"] == 0.5
        assert quiz["average_score"] == 70.0
        final = tasks[str(world["t3"])]
        assert (final["students_submitted"], final["submissions"]) == (2, 2)
        assert final["pass_share"] == 0.5
        assert final["average_score"] == 52.5

    async def test_the_hardest_criteria_are_the_last_attempts_musts_and_shoulds(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        t1 = (await _tasks(ac, world))[str(world["t1"])]
        hardest = t1["hardest_criteria"]
        assert all(set(c) == CRITERION_KEYS for c in hardest)
        # By the last attempts: c1 missed by Bob and Dan, c4 by Bob and Dan,
        # c2 by Cyd. c3 (a "may") is missed by two, and is not listed. Ann's
        # last attempt is a copy of her second, which met everything; the
        # misses of her first (and Bob's first) do not count. c1 before c4:
        # the same share, the list's order.
        assert [
            (c["criterion_id"], c["weight"], c["not_met_students"], c["share"])
            for c in hardest
        ] == [
            ("c1", "must", 2, 0.5),
            ("c4", "should", 2, 0.5),
            ("c2", "should", 1, 0.25),
        ]
        assert [c["text"] for c in hardest] == [
            "Handles an empty list",
            "Covers the edge cases",
            "Explains the idea",
        ]
        assert {c["judged_students"] for c in hardest} == {4}

    async def test_a_may_is_never_a_hardest_criterion(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        for task in (await _tasks(ac, world)).values():
            assert "may" not in {c["weight"] for c in task["hardest_criteria"]}
        t1 = (await _tasks(ac, world))[str(world["t1"])]
        assert "c3" not in {c["criterion_id"] for c in t1["hardest_criteria"]}

    async def test_a_miss_only_in_an_early_attempt_is_not_counted(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        t1 = (await _tasks(ac, world))[str(world["t1"])]
        by_id = {c["criterion_id"]: c for c in t1["hardest_criteria"]}
        # Ann missed c2 in her first attempt and Bob in his first; both met it
        # in the last. Only Cyd's last attempt misses it.
        assert by_id["c2"]["not_met_students"] == 1
        # Ann missed c1 first and met it after; Bob and Dan miss it last.
        assert by_id["c1"]["not_met_students"] == 2

    async def test_a_task_with_a_second_list_reads_only_the_list_in_force(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        tasks = await _tasks(ac, world)
        # The project was judged against a list that was replaced since; its
        # verdicts name another list, so no criterion of the one in force is
        # counted.
        assert tasks[str(world["t3"])]["hardest_criteria"] == []
        loops = tasks[str(world["t2"])]["hardest_criteria"]
        assert [(c["criterion_id"], c["share"]) for c in loops] == [("c1", 0.5)]

    async def test_a_test_has_no_hardest_criteria(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        assert (await _tasks(ac, world))[str(world["quiz"])]["hardest_criteria"] == []

    async def test_a_service_student_is_left_out_and_counted(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        body = (await ac.get(_summary(world["course"]))).json()
        assert body["include_test_students"] is False
        assert body["excluded_test_students"] == 1
        t1 = {t["task_id"]: t for t in body["tasks"]}[str(world["t1"])]
        assert (t1["students_submitted"], t1["submissions"]) == (4, 7)

    async def test_a_service_student_is_counted_when_asked_for(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        body = (await ac.get(_summary(world["course"], True))).json()
        assert body["include_test_students"] is True
        assert body["excluded_test_students"] == 0
        t1 = {t["task_id"]: t for t in body["tasks"]}[str(world["t1"])]
        assert (t1["students_submitted"], t1["submissions"]) == (5, 8)
        assert t1["pass_share"] == 0.4  # 2 of 5
        assert t1["average_score"] == 55.0  # (265 + 10) / 5
        # The tester misses c1..c4 in the last attempt: c1 and c4 now 3 of 5.
        by_id = {c["criterion_id"]: c for c in t1["hardest_criteria"]}
        assert (by_id["c1"]["not_met_students"], by_id["c1"]["share"]) == (3, 0.6)

    async def test_the_default_leaves_the_service_students_out(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        explicit = (await ac.get(_summary(world["course"], False))).json()
        default = (
            await ac.get(f"/api/v1/nodes/{world['course']}/homework/summary")
        ).json()
        assert explicit == default

    async def test_a_course_with_no_work_lists_its_tasks_empty(
        self,
        client: Client,
        world: dict[str, Any],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ac, _ = client
        async with session_factory() as s:
            empty = make_root_course_node(
                tenant_id=world["tenant"], title="Empty", order=5
            )
            s.add(empty)
            await s.flush()
            task = AuthoredDocument(
                course_node_id=empty.id,
                course_root_id=empty.id,
                source_type="text",
                source_url="https://example.com/e",
                title="Nobody's",
                task_type="task",
            )
            s.add(task)
            await s.commit()
            empty_id, task_id = empty.id, task.id
        try:
            body = (await ac.get(_summary(empty_id))).json()
            [only] = body["tasks"]
            assert only["task_id"] == str(task_id)
            assert only["students_submitted"] == 0
            assert only["submissions"] == 0
            assert only["pass_share"] is None
            assert only["average_score"] is None
            assert only["attempts_to_pass"] is None
            assert only["hardest_criteria"] == []
            table = (await ac.get(_table(empty_id))).json()
            assert table["students"] == []
            assert table["total"] == 0
        finally:
            async with session_factory() as s:
                await s.execute(
                    delete(AuthoredDocument).where(AuthoredDocument.id == task_id)
                )
                await s.execute(delete(CourseNode).where(CourseNode.id == empty_id))
                await s.commit()


# ── The table ───────────────────────────────────────────────────────────────


class TestTable:
    async def test_the_columns_are_the_summarys_tasks_in_the_same_order(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        summary = (await ac.get(_summary(world["course"]))).json()["tasks"]
        table = (await ac.get(_table(world["course"]))).json()
        assert set(table) == TABLE_KEYS
        assert all(set(c) == COLUMN_KEYS for c in table["tasks"])
        assert table["tasks"] == [{k: t[k] for k in COLUMN_KEYS} for t in summary]

    async def test_the_rows_are_the_enrolled_and_the_ones_who_submitted_by_name(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        body = (await ac.get(_table(world["course"]))).json()
        assert all(set(r) == ROW_KEYS for r in body["students"])
        # Eva submitted without enrolling; Fay is enrolled and never submitted;
        # the nameless one is last. The service student, the deleted one and
        # the other tenant's student are no rows.
        assert [r["student_id"] for r in body["students"]] == [
            str(world[n])
            for n in ("ann", "bob", "cyd", "dan", "eva", "fay", "nameless")
        ]
        assert [r["display_name"] for r in body["students"]] == [
            "Ann", "Bob", "Cyd", "Dan", "Eva", "Fay", None,
        ]  # fmt: skip
        assert body["students"][0]["login"] == "ann-login"
        assert body["students"][1]["login"] is None
        assert (body["total"], body["limit"], body["offset"]) == (7, 500, 0)
        assert body["excluded_test_students"] == 1

    async def test_the_four_states_of_a_cell(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        rows = {
            r["student_id"]: r["cells"]
            for r in (await ac.get(_table(world["course"]))).json()["students"]
        }

        def cell(student: str, task: str) -> dict[str, Any]:
            found: dict[str, Any] = rows[str(world[student])][str(world[task])]
            return found

        assert cell("ann", "t1")["state"] == "passed"
        assert cell("bob", "t1")["state"] == "not_passed"
        assert cell("bob", "quiz")["state"] == "in_review"
        assert cell("dan", "t2")["state"] == "in_review"  # held for funds
        assert cell("dan", "quiz")["state"] == "not_submitted"
        # Passed beats a new submission in the work.
        assert cell("ann", "t2")["state"] == "passed"
        # A refusal is no submission.
        assert cell("cyd", "t3")["state"] == "not_submitted"
        assert {c["state"] for cells in rows.values() for c in cells.values()} == {
            "not_submitted",
            "in_review",
            "passed",
            "not_passed",
        }
        for cells in rows.values():
            assert set(cells) == {str(world[t]) for t in ("t1", "t2", "quiz", "t3")}
            assert all(set(c) == CELL_KEYS for c in cells.values())

    async def test_a_cell_carries_the_best_score_the_attempts_and_the_last_time(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        rows = {
            r["student_id"]: r["cells"]
            for r in (await ac.get(_table(world["course"]))).json()["students"]
        }
        ann = rows[str(world["ann"])][str(world["t1"])]
        assert (ann["best_score"], ann["attempts"]) == (90, 3)
        bob = rows[str(world["bob"])][str(world["t1"])]
        assert (bob["best_score"], bob["attempts"]) == (65, 2)
        # In the work, not yet an attempt: no score, and the time is the submission's.
        waiting = rows[str(world["bob"])][str(world["quiz"])]
        assert (waiting["best_score"], waiting["attempts"]) == (None, 0)
        assert waiting["last_submitted_at"] is not None
        # Ann's Loops: one attempt, and a second submission in the work.
        loops = rows[str(world["ann"])][str(world["t2"])]
        assert loops["attempts"] == 1
        never = rows[str(world["fay"])][str(world["t1"])]
        assert never == {
            "state": "not_submitted",
            "best_score": None,
            "attempts": 0,
            "last_submitted_at": None,
        }
        assert ann["last_submitted_at"] is not None

    async def test_the_last_time_is_the_latest_submission_of_the_cell(
        self,
        client: Client,
        world: dict[str, Any],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ac, _ = client
        async with session_factory() as s:
            rows = (
                (
                    await s.execute(
                        select(HomeworkSubmission.created_at)
                        .where(
                            HomeworkSubmission.student_id == world["ann"],
                            HomeworkSubmission.authored_document_id == world["t2"],
                            HomeworkSubmission.tenant_id == world["tenant"],
                        )
                        .order_by(HomeworkSubmission.created_at)
                    )
                )
                .scalars()
                .all()
            )
        cells = {
            r["student_id"]: r["cells"]
            for r in (await ac.get(_table(world["course"]))).json()["students"]
        }
        shown = cells[str(world["ann"])][str(world["t2"])]["last_submitted_at"]
        assert datetime.fromisoformat(shown) == rows[-1]

    async def test_a_service_student_is_a_row_when_asked_for(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        body = (
            await ac.get(_table(world["course"], "?include_test_students=true"))
        ).json()
        assert body["include_test_students"] is True
        assert body["excluded_test_students"] == 0
        assert body["total"] == 8
        names = [r["display_name"] for r in body["students"]]
        assert names == ["Ann", "Bob", "Cyd", "Dan", "Eva", "Fay", "Test Student", None]
        assert str(world["gone"]) not in {r["student_id"] for r in body["students"]}

    async def test_pages_by_limit_and_offset(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        page = (await ac.get(_table(world["course"], "?limit=2&offset=2"))).json()
        assert [r["display_name"] for r in page["students"]] == ["Cyd", "Dan"]
        assert (page["total"], page["limit"], page["offset"]) == (7, 2, 2)
        # The cells of a page are those of its own students only.
        assert all(
            set(r["cells"]) == {str(world[t]) for t in ("t1", "t2", "quiz", "t3")}
            for r in page["students"]
        )
        last = (await ac.get(_table(world["course"], "?limit=2&offset=6"))).json()
        assert [r["display_name"] for r in last["students"]] == [None]
        beyond = (await ac.get(_table(world["course"], "?offset=50"))).json()
        assert beyond["students"] == [] and beyond["total"] == 7

    @pytest.mark.parametrize("query", ["?limit=0", "?limit=501", "?offset=-1"])
    async def test_a_page_outside_the_bounds_is_422(
        self, client: Client, world: dict[str, Any], query: str
    ) -> None:
        ac, _ = client
        assert (await ac.get(_table(world["course"], query))).status_code == 422

    async def test_a_cell_agrees_with_what_the_student_is_shown(
        self,
        client: Client,
        world: dict[str, Any],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Passed is the portal's verdict for the same review, row by row."""
        ac, _ = client
        cells = {
            r["student_id"]: r["cells"]
            for r in (await ac.get(_table(world["course"]))).json()["students"]
        }
        async with session_factory() as s:
            attempts = (
                (
                    await s.execute(
                        select(HomeworkSubmission).where(
                            HomeworkSubmission.tenant_id == world["tenant"],
                            HomeworkSubmission.course_node_id == world["course"],
                            HomeworkSubmission.deleted_at.is_(None),
                            HomeworkSubmission.status.in_(ATTEMPT_STATUSES),
                        )
                    )
                )
                .scalars()
                .all()
            )
        passed_by_portal: dict[tuple[uuid.UUID, uuid.UUID], bool] = {}
        for sub in attempts:
            verdict = curated_verdict(
                sub.review_result, score=sub.score, submission_id=sub.id
            )
            key = (sub.student_id, sub.authored_document_id)
            passed_by_portal[key] = passed_by_portal.get(key, False) or bool(
                verdict is not None and verdict.passed
            )
        for (student_id, task_id), passed in passed_by_portal.items():
            row = cells.get(str(student_id))
            if row is None:  # service student, deleted student, other tenant
                continue
            assert (row[str(task_id)]["state"] == "passed") is passed


# ── What never leaves ───────────────────────────────────────────────────────


class TestNoTexts:
    async def test_no_text_of_a_work_a_review_or_a_quote_reaches_either_answer(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        for path in (
            _summary(world["course"]),
            _summary(world["course"], True),
            _table(world["course"]),
            _table(world["course"], "?include_test_students=true"),
        ):
            response = await ac.get(path)
            assert response.status_code == 200
            assert _MARKER not in response.text, path
        # The marker is where the texts live: the seed did plant it.
        # (The student's file name, the review, the note, the quote, the miss.)


# ── Speed ───────────────────────────────────────────────────────────────────

SUMMARY_QUERIES = 8
"""The course, its tree, its tasks, the two lists in force, the works, the
tally of criteria, the count of service students left out."""
TABLE_QUERIES = 7
"""The course, its tree, its tasks, the page of students, their count, the
count of service students left out, the works of the page."""


class _Counter:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine.sync_engine
        self.statements: list[str] = []

    def _spy(self, _c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        self.statements.append(statement)

    def __enter__(self) -> _Counter:
        event.listen(self._engine, "before_cursor_execute", self._spy)
        return self

    def __exit__(self, *_exc: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._spy)


class TestQueries:
    async def test_the_number_of_queries_is_fixed_by_the_shape_of_the_routes(
        self,
        client: Client,
        world: dict[str, Any],
        async_engine: AsyncEngine,
    ) -> None:
        ac, _ = client
        with _Counter(async_engine) as summary:
            assert (await ac.get(_summary(world["course"]))).status_code == 200
        with _Counter(async_engine) as table:
            assert (await ac.get(_table(world["course"]))).status_code == 200
        assert len(summary.statements) == SUMMARY_QUERIES
        assert len(table.statements) == TABLE_QUERIES

    async def test_fifty_students_by_ten_tasks_cost_the_same_queries(
        self,
        client: Client,
        world: dict[str, Any],
        session_factory: async_sessionmaker[AsyncSession],
        async_engine: AsyncEngine,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        ac, _ = client
        async with session_factory() as s:
            big = make_root_course_node(tenant_id=world["tenant"], title="Big", order=9)
            s.add(big)
            await s.flush()
            tasks = [
                AuthoredDocument(
                    course_node_id=big.id,
                    course_root_id=big.id,
                    source_type="text",
                    source_url=f"https://example.com/big{n}",
                    title=f"Task {n}",
                    task_type="task",
                    order=n,
                    content_hash=f"hb{n}",
                )
                for n in range(10)
            ]
            s.add_all(tasks)
            await s.flush()
            lists = [
                TaskCriteriaOverride(
                    authored_document_id=t.id,
                    source_content_hash=t.content_hash,
                    source_task_type="task",
                    criteria=criteria_to_document(_LIST_T1),
                )
                for t in tasks
            ]
            s.add_all(lists)
            students = [
                Student(
                    tenant_id=world["tenant"],
                    external_id=f"big-{n}",
                    display_name=f"Student {n:02d}",
                )
                for n in range(50)
            ]
            s.add_all(students)
            await s.flush()
            s.add_all(
                StudentEnrollment(student_id=p.id, course_node_id=big.id)
                for p in students
            )
            stamp = _T0
            weights = {"c1": "must", "c2": "should", "c3": "may", "c4": "should"}
            for si, student in enumerate(students):
                for ti, (task, listed) in enumerate(zip(tasks, lists, strict=True)):
                    for attempt_no in (1, 2, 3):
                        passed = (si + ti + attempt_no) % 3 == 0
                        stamp += timedelta(minutes=1)
                        sub = HomeworkSubmission(
                            tenant_id=world["tenant"],
                            student_id=student.id,
                            course_node_id=big.id,
                            node_id=big.id,
                            authored_document_id=task.id,
                            file_url="s3://bucket/work.py",
                            file_type="text/plain",
                            delivery_mode="in_app",
                            status="completed",
                            score=50 + attempt_no * 10,
                            attempt_no=attempt_no,
                            review_result=_review(passed=passed, score=60),
                            created_at=stamp,
                        )
                        s.add(sub)
                        await s.flush()
                        s.add_all(
                            SubmissionCriterionVerdict(
                                tenant_id=world["tenant"],
                                submission_id=sub.id,
                                criteria_layer=CriteriaLayer.AUTHOR.value,
                                criteria_source_id=listed.id,
                                item_id=cid,
                                item_kind=VerdictItemKind.CRITERION.value,
                                weight=weights[cid],
                                verdict=VerdictValue.NOT_MET.value,
                                model_verdict=VerdictValue.NOT_MET.value,
                                missing="gap",
                                quote_not_found=False,
                            )
                            for cid in weights
                        )
            await s.commit()
            big_id = big.id
        try:
            with _Counter(async_engine) as summary:
                started = time.perf_counter()
                response = await ac.get(_summary(big_id))
                summary_s = time.perf_counter() - started
            assert response.status_code == 200
            with _Counter(async_engine) as table:
                started = time.perf_counter()
                response = await ac.get(_table(big_id))
                table_s = time.perf_counter() - started
            assert response.status_code == 200
            body = response.json()
            assert (len(body["students"]), len(body["tasks"])) == (50, 10)
            assert len(summary.statements) == SUMMARY_QUERIES
            assert len(table.statements) == TABLE_QUERIES
            with capsys.disabled():
                print(
                    f"\n[M-10 50x10] summary: {len(summary.statements)} queries, "
                    f"{summary_s * 1000:.0f} ms; table: {len(table.statements)} "
                    f"queries, {table_s * 1000:.0f} ms"
                )
        finally:
            async with session_factory() as s:
                task_ids = select(AuthoredDocument.id).where(
                    AuthoredDocument.course_root_id == big_id
                )
                subs = select(HomeworkSubmission.id).where(
                    HomeworkSubmission.course_node_id == big_id
                )
                await s.execute(
                    delete(SubmissionCriterionVerdict).where(
                        SubmissionCriterionVerdict.submission_id.in_(subs)
                    )
                )
                await s.execute(
                    delete(HomeworkSubmission).where(
                        HomeworkSubmission.course_node_id == big_id
                    )
                )
                await s.execute(
                    delete(StudentEnrollment).where(
                        StudentEnrollment.course_node_id == big_id
                    )
                )
                await s.execute(
                    delete(TaskCriteriaOverride).where(
                        TaskCriteriaOverride.authored_document_id.in_(task_ids)
                    )
                )
                await s.execute(
                    delete(AuthoredDocument).where(
                        AuthoredDocument.course_root_id == big_id
                    )
                )
                await s.execute(delete(CourseNode).where(CourseNode.id == big_id))
                await s.commit()
