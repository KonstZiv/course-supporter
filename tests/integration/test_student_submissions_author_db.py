"""The author's screen of a student's work, through its three routes (M-3, 24.11).

``GET /students/{id}/submissions``, ``.../{submission_id}`` and ``.../file``,
on a live database, with the key context of a tenant that carries scope PREP.
The world is two tenants, so the boundary is tested where it can fail: the
student of another tenant, the submission of another student, a soft-deleted
student or submission, an identifier nobody has — every one of them is the one
404 of its route.

Locks, as the doc of the task asks for them:

* the keys of both responses are an explicit white list — a field of the
  investigation trace cannot reach the author by growing the schema;
* the link to a file never reaches the log, nor the key of the object;
* ``passed`` and ``presentation`` are the portal's, for the same submission.

Requires ``docker compose up -d`` (or a local PostgreSQL); run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from course_supporter.api.app import app
from course_supporter.api.deps import (
    get_current_student,
    get_current_tenant,
    get_s3_client,
)
from course_supporter.api.routes._portal_shared import (
    curated_presentation,
    curated_verdict,
)
from course_supporter.api.routes.student_submissions import FILE_LINK_TTL_SEC
from course_supporter.auth.context import StudentContext, TenantContext
from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.feedback_kinds import FeedbackTargetKind
from course_supporter.homework import path_config
from course_supporter.homework.criteria_form import (
    Criterion,
    MandatoryPoint,
    criteria_to_document,
)
from course_supporter.models import review_structure
from course_supporter.models.review_schema import REVIEW_SCHEMA_VERSION
from course_supporter.models.review_structure import (
    PassRule,
    ReviewStructureV1,
    Verdict,
)
from course_supporter.models.source import AssignmentType
from course_supporter.normalizer import (
    EntryClass,
    Manifest,
    ManifestEntry,
    manifest_to_jsonb,
)
from course_supporter.remark_kinds import CommentAuthor, CommentKind
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    Job,
    ProjectBase,
    Student,
    StudentCredential,
    StudentEnrollment,
    StudentFeedback,
    SubmissionCriterionVerdict,
    TaskCriteriaOverride,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node
from tests._helpers.path_config import shipped_with_replies_for
from tests._helpers.remark_comments import a_stored_comment

pytestmark = pytest.mark.requires_db

_T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
_URL_MARKER = "URL-MARKER-8c1d"
_TRACE_MARKER = "TRACE-MARKER-5e7a"

STUDENT_NOT_FOUND = "Student not found."
SUBMISSION_NOT_FOUND = "Submission not found."

# The white lists: every key a response may carry, and nothing else.
_BASE_KEYS = {
    "id",
    "attempt_no",
    "previous_submission_id",
    "created_at",
    "reviewed_at",
    "course_id",
    "course_title",
    "task_document_id",
    "task_title",
    "task_type",
    "status",
    "presentation",
    "score",
    "rejection",
    "not_opened",
    "recovered_encoding",
    "is_repeat_copy",
    "review_language",
    "original_filename",
}
LIST_ITEM_KEYS = _BASE_KEYS | {"passed"}
LIST_KEYS = {"student", "enrollments", "items", "total", "limit", "offset"}
LIST_STUDENT_KEYS = {"student_id", "display_name", "login"}
ENROLLMENT_KEYS = {"course_id", "course_title", "enrolled_at"}
DETAIL_KEYS = _BASE_KEYS | {
    "student",
    "verdict",
    "structure",
    "review_markdown",
    "delta",
    "own_feedback",
    "pass_rule",
    "criteria",
}
DETAIL_STUDENT_KEYS = {"student_id", "display_name"}
PASS_RULE_KEYS = {"basis", "threshold", "required_musts"}
CRITERION_KEYS = {
    "item_id",
    "item_kind",
    "text",
    "weight",
    "must_by_author",
    "verdict",
    "quote",
    "quote_line_start",
    "missing",
    "remark_state",
}
FILE_LINK_KEYS = {"url", "expires_at"}


def _criterion(
    cid: str,
    text: str,
    weight: str,
    *,
    must_by_author: bool,
    points: tuple[str, ...] = (),
) -> Criterion:
    return Criterion(
        id=cid,
        text=text,
        evidence=f"evidence of {cid}",
        weight=WeightCategory(weight),
        check_method="mandatory_points" if points else "model_verdict",
        soft_descent=False,
        concepts=(),
        mandatory_points=tuple(
            MandatoryPoint(id=f"{cid}.p{n}", text=p) for n, p in enumerate(points, 1)
        ),
        must_by_author=must_by_author,
    )


# The author's list: c1 a "must" the author made mandatory, c2 checked by two
# points, c3 a "may".
_LIST = (
    _criterion("c1", "Handles an empty list", "must", must_by_author=True),
    _criterion(
        "c2",
        "Explains the idea",
        "should",
        must_by_author=False,
        points=("Names the algorithm", "Gives its cost"),
    ),
    _criterion("c3", "Uses type hints", "may", must_by_author=False),
)


def _review(
    *,
    passed: bool,
    basis: str,
    score: int,
    threshold: int,
    language: str = "ukr",
    unchanged_from: int | None = None,
) -> dict[str, Any]:
    structure = ReviewStructureV1(
        schema_version=REVIEW_SCHEMA_VERSION,
        language=language,
        verdict=Verdict(
            passed=passed,
            why="Because of the list.",
            rule=PassRule(
                basis=basis,
                score=score,
                threshold=threshold,
            ),
        ),
        unchanged_from_attempt=unchanged_from,
    )
    return structure.model_dump(mode="json")


def _verdict_row(
    submission: HomeworkSubmission, source_id: uuid.UUID, item_id: str, **fields: Any
) -> SubmissionCriterionVerdict:
    kind = "point" if "." in item_id else "criterion"
    row: dict[str, Any] = {
        "tenant_id": submission.tenant_id,
        "submission_id": submission.id,
        "criteria_layer": CriteriaLayer.AUTHOR.value,
        "criteria_source_id": source_id,
        "item_id": item_id,
        "item_kind": VerdictItemKind(kind).value,
        "weight": None,
        "verdict": VerdictValue.MET.value,
        "model_verdict": VerdictValue.MET.value,
        "quote": f"quote of {item_id}",
        "quote_file": "solution.py",
        "quote_line_start": 4,
        "quote_line_end": 4,
        "missing": None,
        "quote_not_found": False,
        "remark_state": None,
    }
    row.update(fields)
    return SubmissionCriterionVerdict(**row)


class _FakeS3:
    """The two calls the file route makes; remembers what it was asked."""

    def __init__(self) -> None:
        self.presigned: list[dict[str, Any]] = []

    def extract_key(self, url: str) -> str | None:
        prefix = "s3://bucket/"
        return url[len(prefix) :] if url.startswith(prefix) else None

    async def generate_presigned_get_url(
        self,
        key: str,
        *,
        expires_in: int,
        content_disposition: str | None = None,
        response_content_type: str | None = None,
    ) -> str:
        self.presigned.append(
            {
                "key": key,
                "expires_in": expires_in,
                "content_disposition": content_disposition,
            }
        )
        return f"https://signed.example/{key}?sig={_URL_MARKER}"


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, Any]]:
    """Two tenants, two courses and a student with a history worth reading."""
    async with session_factory() as s:
        tenant = Tenant(name=f"m3-a-{uuid.uuid4().hex[:6]}")
        other = Tenant(name=f"m3-b-{uuid.uuid4().hex[:6]}")
        s.add_all([tenant, other])
        await s.flush()

        c1 = make_root_course_node(tenant_id=tenant.id, title="Course One", order=0)
        c2 = make_root_course_node(tenant_id=tenant.id, title="Course Two", order=1)
        cb = make_root_course_node(tenant_id=other.id, title="Foreign", order=0)
        s.add_all([c1, c2, cb])
        await s.flush()

        def doc(root: CourseNode, **kw: Any) -> AuthoredDocument:
            return AuthoredDocument(
                course_node_id=root.id,
                course_root_id=root.id,
                source_type="text",
                source_url=f"https://example.com/{uuid.uuid4().hex[:6]}",
                **kw,
            )

        task = doc(
            c1,
            title="Sort a list",
            filename="sort.md",
            task_type="task",
            content_hash="v1",
        )
        quiz = doc(c1, title="Quiz", task_type="test", order=2)
        proj = doc(c2, task_type="project", order=7)
        foreign_task = doc(cb, title="Foreign task", task_type="task")
        s.add_all([task, quiz, proj, foreign_task])
        await s.flush()

        ada = Student(tenant_id=tenant.id, external_id="ada", display_name="Ada")
        bob = Student(tenant_id=tenant.id, external_id="bob", display_name="Bob")
        gone = Student(
            tenant_id=tenant.id,
            external_id="gone",
            display_name="Gone",
            deleted_at=_T0,
        )
        eve = Student(tenant_id=other.id, external_id="eve", display_name="Eve")
        s.add_all([ada, bob, gone, eve])
        await s.flush()
        s.add_all(
            [
                StudentCredential(
                    student_id=ada.id,
                    tenant_id=tenant.id,
                    login="ada-login",
                    password_hash="x",
                ),
                StudentEnrollment(student_id=ada.id, course_node_id=c1.id),
                StudentEnrollment(student_id=ada.id, course_node_id=c2.id),
            ]
        )
        override = TaskCriteriaOverride(
            authored_document_id=task.id,
            source_content_hash="h" * 64,
            source_task_type="task",
            criteria=criteria_to_document(_LIST),
        )
        s.add(override)
        await s.flush()

        counter = 0

        async def submit(
            student: Student,
            root: CourseNode,
            on: AuthoredDocument,
            *,
            finished_at: datetime | None = None,
            **fields: Any,
        ) -> HomeworkSubmission:
            nonlocal counter
            counter += 1
            fields.setdefault("original_filename", "work.py")
            fields.setdefault("tenant_id", student.tenant_id)
            row = HomeworkSubmission(
                student_id=student.id,
                course_node_id=root.id,
                node_id=root.id,
                authored_document_id=on.id,
                file_url="s3://bucket/homework/work.py",
                file_type="text/plain",
                delivery_mode="in_app",
                created_at=_T0 + timedelta(hours=counter),
                **fields,
            )
            s.add(row)
            await s.flush()
            if finished_at is not None:
                job = Job(
                    tenant_id=student.tenant_id,
                    course_node_id=root.id,
                    job_type="homework_processing",
                    status="complete",
                    subject_type="homework_submission",
                    subject_id=row.id,
                    completed_at=finished_at,
                )
                s.add(job)
                await s.flush()
                row.job_id = job.id
                await s.flush()
            return row

        failing = _review(
            passed=False, basis="author_musts_unmet", score=40, threshold=70
        )
        passing = _review(passed=True, basis="author_musts_met", score=90, threshold=70)
        a1 = await submit(
            ada,
            c1,
            task,
            status="completed",
            attempt_no=1,
            task_version="v1",
            score=40,
            review_result=failing,
            review_markdown="# Review one",
            response_language="eng",
            finished_at=_T0 + timedelta(days=1, minutes=3),
        )
        a2 = await submit(
            ada,
            c1,
            task,
            status="completed",
            attempt_no=2,
            task_version="v1",
            previous_submission_id=a1.id,
            score=90,
            review_result=passing,
            review_markdown="# Review two",
            finished_at=_T0 + timedelta(days=2, minutes=3),
        )
        a3 = await submit(
            ada,
            c1,
            task,
            status="delivered",
            attempt_no=3,
            task_version="v1",
            previous_submission_id=a2.id,
            score=90,
            review_result=_review(
                passed=True,
                basis="author_musts_met",
                score=90,
                threshold=70,
                unchanged_from=2,
            ),
            attempt_receipt={"attempt_no": 3, "dedup_of": 2},
            finished_at=_T0 + timedelta(days=3, minutes=3),
        )
        old = await submit(
            ada,
            c1,
            task,
            status="completed",
            score=90,
            review_result={
                "verdict": {"passed": True, "correctness": "correct"},
                "layers": [{"internal": _TRACE_MARKER}],
            },
            review_markdown="# Old review",
            response_language="eng",
            safety_result={"source": "stage1", "internal": _TRACE_MARKER},
            sanity_result={"verdict": "match", "internal": _TRACE_MARKER},
            error_message=_TRACE_MARKER,
        )
        rejected = await submit(
            ada,
            c1,
            task,
            status="rejected",
            safety_result={
                "source": "stage1",
                "category": "unsupported_extension",
                "not_opened": [{"arcname": "a.exe", "reason": "magic", "size": 3}],
                "internal": _TRACE_MARKER,
            },
            sanity_result={"internal": _TRACE_MARKER},
            # A job that ended is not a review: the status decides.
            finished_at=_T0 + timedelta(days=5),
        )
        base = ProjectBase(
            authored_document_id=proj.id,
            version=1,
            archive_key="bases/x/original.zip",
            state="ready",
            manifest=manifest_to_jsonb(
                Manifest(
                    schema=1,
                    aggregate_hash="a" * 64,
                    included=(
                        ManifestEntry("main.py", 5, "b" * 64, EntryClass.TEXT),
                        ManifestEntry("old.py", 5, "c" * 64, EntryClass.TEXT),
                    ),
                    excluded=(),
                    total_files=2,
                    total_bytes=10,
                )
            ),
        )
        s.add(base)
        await s.flush()
        project = await submit(
            ada,
            c2,
            proj,
            status="completed",
            attempt_no=1,
            score=85,
            base_id=base.id,
            review_result=_review(
                passed=True, basis="no_author_musts_passed", score=85, threshold=80
            ),
            snapshot_manifest=manifest_to_jsonb(
                Manifest(
                    schema=1,
                    aggregate_hash="d" * 64,
                    included=(
                        ManifestEntry("main.py", 9, "e" * 64, EntryClass.TEXT),
                        ManifestEntry("new.py", 3, "f" * 64, EntryClass.TEXT),
                    ),
                    excluded=(),
                    total_files=2,
                    total_bytes=12,
                )
            ),
            finished_at=_T0 + timedelta(days=6),
        )
        quiz_sub = await submit(
            ada,
            c1,
            quiz,
            status="completed",
            score=50,
            review_result=ReviewStructureV1(
                schema_version=REVIEW_SCHEMA_VERSION,
                language="ukr",
                verdict=Verdict(passed=True),
                test=review_structure.TestSection(
                    score=50,
                    questions=[
                        review_structure.TestQuestionResult(number="1", correct=True)
                    ],
                ),
            ).model_dump(mode="json"),
            original_filename="answers.json",
        )
        deleted = await submit(
            ada, c1, task, status="completed", score=10, deleted_at=_T0
        )
        bobs = await submit(bob, c1, task, status="completed", score=70, attempt_no=1)
        eves = await submit(
            eve, cb, foreign_task, status="completed", score=70, attempt_no=1
        )
        goners = await submit(gone, c1, task, status="completed", score=70)
        # A row whose student is this tenant's but whose own tenant is another:
        # the boundary reads the submission's tenant, not only the student's.
        smuggled = await submit(
            ada, cb, foreign_task, status="completed", score=70, tenant_id=other.id
        )

        # Verdict rows of the first and the second attempt, by the author's list.
        s.add_all(
            [
                _verdict_row(
                    a1,
                    override.id,
                    "c1",
                    weight="must",
                    verdict="not_met",
                    model_verdict="not_met",
                    quote=None,
                    quote_file=None,
                    quote_line_start=None,
                    quote_line_end=None,
                    missing="Nothing handles [].",
                    remark_state="new",
                ),
                _verdict_row(
                    a1,
                    override.id,
                    "c2",
                    weight="should",
                    model_verdict=None,
                    quote=None,
                    quote_file=None,
                    quote_line_start=None,
                    quote_line_end=None,
                ),
                _verdict_row(a1, override.id, "c2.p1"),
                _verdict_row(
                    a1,
                    override.id,
                    "c2.p2",
                    verdict="not_met",
                    model_verdict="not_met",
                    quote=None,
                    quote_file=None,
                    quote_line_start=None,
                    quote_line_end=None,
                    missing="No cost given.",
                ),
                _verdict_row(
                    a1,
                    override.id,
                    "c3",
                    weight="may",
                    verdict="not_met",
                    quote_not_found=True,
                    quote="def f(x) -> int",
                    quote_file=None,
                    quote_line_start=None,
                    quote_line_end=None,
                ),
                _verdict_row(
                    a2, override.id, "c1", weight="must", remark_state="fixed"
                ),
                _verdict_row(
                    a2, override.id, "c2", weight="should", model_verdict=None
                ),
                _verdict_row(a2, override.id, "c2.p1"),
                _verdict_row(a2, override.id, "c2.p2"),
                _verdict_row(
                    a2,
                    override.id,
                    "c3",
                    weight="may",
                    remark_state="fixed",
                ),
            ]
        )
        await s.flush()
        # The talk under the review: two replies of the student on the first
        # attempt, and the Mentor's answer to the first in the second review.
        question = await a_stored_comment(s, a1, item_id="c1", minute=0)
        await a_stored_comment(
            s,
            a1,
            item_id="c2",
            kind=CommentKind.OBJECTION,
            body="Я не згоден: алгоритм названо в другому абзаці.",
            minute=1,
        )
        await a_stored_comment(
            s,
            a2,
            item_id="c1",
            author=CommentAuthor.MENTOR,
            kind=CommentKind.ANSWER,
            body="Перевірку порожнього списку тепер видно в рядку 4.",
            parent_id=question.id,
            minute=2,
        )
        s.add(
            StudentFeedback(
                tenant_id=tenant.id,
                student_id=ada.id,
                target_kind=FeedbackTargetKind.REVIEW.value,
                target_id=a2.id,
                kind="touch",
                value="helped",
            )
        )
        await s.commit()
        ids = {
            "tenant": tenant.id,
            "other_tenant": other.id,
            "c1": c1.id,
            "c2": c2.id,
            "task": task.id,
            "proj": proj.id,
            "ada": ada.id,
            "bob": bob.id,
            "eve": eve.id,
            "gone": gone.id,
            "a1": a1.id,
            "a2": a2.id,
            "a3": a3.id,
            "old": old.id,
            "rejected": rejected.id,
            "project": project.id,
            "quiz": quiz_sub.id,
            "deleted": deleted.id,
            "bobs": bobs.id,
            "eves": eves.id,
            "goners": goners.id,
            "smuggled": smuggled.id,
            "override": override.id,
        }

    yield ids

    async with session_factory() as s:
        tenants = [ids["tenant"], ids["other_tenant"]]
        subs = select(HomeworkSubmission.id).where(
            HomeworkSubmission.tenant_id.in_(tenants)
        )
        await s.execute(
            delete(SubmissionCriterionVerdict).where(
                SubmissionCriterionVerdict.submission_id.in_(subs)
            )
        )
        await s.execute(
            delete(StudentFeedback).where(StudentFeedback.tenant_id.in_(tenants))
        )
        await s.execute(
            delete(HomeworkSubmission).where(HomeworkSubmission.tenant_id.in_(tenants))
        )
        await s.execute(delete(Job).where(Job.tenant_id.in_(tenants)))
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
        await s.execute(
            delete(ProjectBase).where(ProjectBase.authored_document_id.in_(docs))
        )
        await s.execute(delete(AuthoredDocument).where(AuthoredDocument.id.in_(docs)))
        await s.execute(delete(CourseNode).where(CourseNode.id.in_(roots)))
        await s.execute(delete(Tenant).where(Tenant.id.in_(tenants)))
        await s.commit()


def _key(tenant_id: uuid.UUID, *scopes: str) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        tenant_name="m3",
        scopes=list(scopes),
        plan_id="basic",
        key_prefix="cs_m3",
    )


@pytest.fixture()
def s3() -> _FakeS3:
    return _FakeS3()


@pytest.fixture()
async def client(
    world: dict[str, Any],
    s3: _FakeS3,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[AsyncClient, Callable[[TenantContext], None]]]:
    async def _yield_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _yield_session
    app.dependency_overrides[get_current_tenant] = lambda: _key(world["tenant"], "prep")
    app.dependency_overrides[get_s3_client] = lambda: s3

    def use_key(ctx: TenantContext) -> None:
        app.dependency_overrides[get_current_tenant] = lambda: ctx

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac, use_key
    app.dependency_overrides.clear()


Client = tuple[AsyncClient, Callable[[TenantContext], None]]


def _list(student: uuid.UUID) -> str:
    return f"/api/v1/students/{student}/submissions"


def _detail(student: uuid.UUID, submission: uuid.UUID) -> str:
    return f"/api/v1/students/{student}/submissions/{submission}"


def _file(student: uuid.UUID, submission: uuid.UUID) -> str:
    return f"{_detail(student, submission)}/file"


def _comments(student: uuid.UUID, submission: uuid.UUID) -> str:
    return f"{_detail(student, submission)}/comments"


# ── The boundary ────────────────────────────────────────────────────────────


class TestBoundary:
    async def test_the_list_of_a_student_of_another_tenant_is_404(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        resp = await ac.get(_list(world["eve"]))
        assert resp.status_code == 404
        assert resp.json() == {"detail": STUDENT_NOT_FOUND}

    async def test_unknown_and_soft_deleted_students_read_alike(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        unknown = await ac.get(_list(uuid.uuid4()))
        gone = await ac.get(_list(world["gone"]))
        foreign = await ac.get(_list(world["eve"]))
        assert {r.status_code for r in (unknown, gone, foreign)} == {404}
        assert unknown.json() == gone.json() == foreign.json()

    @pytest.mark.parametrize(
        ("student", "submission"),
        [
            ("eve", "eves"),  # another tenant's student and work
            ("ada", "eves"),  # another tenant's work under this tenant's student
            ("ada", "bobs"),  # another student's work, same tenant
            ("bob", "a1"),  # this student's work under another student
            ("ada", "smuggled"),  # this student's id, another tenant's row
            ("ada", "deleted"),  # soft-deleted work
            ("gone", "goners"),  # soft-deleted student
            ("ada", None),  # an identifier nobody has
        ],
    )
    async def test_detail_and_file_give_one_404_for_every_way_of_not_being_there(
        self,
        client: Client,
        world: dict[str, Any],
        student: str,
        submission: str | None,
    ) -> None:
        ac, _ = client
        sid = world[submission] if submission else uuid.uuid4()
        detail = await ac.get(_detail(world[student], sid))
        link = await ac.get(_file(world[student], sid))
        talk = await ac.get(_comments(world[student], sid))
        assert detail.status_code == link.status_code == talk.status_code == 404
        assert detail.json() == link.json() == talk.json()
        assert talk.json() == {"detail": SUBMISSION_NOT_FOUND}

    async def test_an_unknown_student_gives_the_same_404_as_a_foreign_submission(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        no_student = await ac.get(_detail(uuid.uuid4(), world["a1"]))
        foreign = await ac.get(_detail(world["ada"], world["bobs"]))
        assert no_student.status_code == foreign.status_code == 404
        assert no_student.json() == foreign.json()

    async def test_a_key_without_scope_prep_is_403_as_in_the_neighbours(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, use_key = client
        use_key(_key(world["tenant"], "check"))
        neighbour = await ac.get("/api/v1/students")
        for path in (
            _list(world["ada"]),
            _detail(world["ada"], world["a1"]),
            _file(world["ada"], world["a1"]),
            _comments(world["ada"], world["a1"]),
        ):
            assert (await ac.get(path)).status_code == neighbour.status_code == 403

    async def test_a_student_bearer_without_a_key_is_refused_as_in_the_neighbours(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        app.dependency_overrides.pop(get_current_tenant)
        headers = {"Authorization": "Bearer student-session-token"}
        neighbour = await ac.get("/api/v1/students", headers=headers)
        assert neighbour.status_code in (401, 403)
        for path in (
            _list(world["ada"]),
            _detail(world["ada"], world["a1"]),
            _file(world["ada"], world["a1"]),
            _comments(world["ada"], world["a1"]),
        ):
            assert (
                await ac.get(path, headers=headers)
            ).status_code == neighbour.status_code


# ── The list ────────────────────────────────────────────────────────────────


class TestList:
    async def test_the_head_names_the_student_and_the_courses(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        body = (await ac.get(_list(world["ada"]))).json()
        assert set(body) == LIST_KEYS
        assert body["student"] == {
            "student_id": str(world["ada"]),
            "display_name": "Ada",
            "login": "ada-login",
        }
        assert [(e["course_id"], e["course_title"]) for e in body["enrollments"]] == [
            (str(world["c1"]), "Course One"),
            (str(world["c2"]), "Course Two"),
        ]
        assert all(set(e) == ENROLLMENT_KEYS for e in body["enrollments"])

    async def test_a_student_without_a_credential_has_a_null_login(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        body = (await ac.get(_list(world["bob"]))).json()
        assert body["student"]["login"] is None
        assert body["enrollments"] == []
        assert [i["id"] for i in body["items"]] == [str(world["bobs"])]

    async def test_newest_first_with_the_total_and_without_the_deleted(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        body = (await ac.get(_list(world["ada"]))).json()
        assert [i["id"] for i in body["items"]] == [
            str(world[k])
            for k in ("quiz", "project", "rejected", "old", "a3", "a2", "a1")
        ]
        assert body["total"] == 7
        assert (body["limit"], body["offset"]) == (20, 0)
        assert all(set(i) == LIST_ITEM_KEYS for i in body["items"])

    async def test_pages(self, client: Client, world: dict[str, Any]) -> None:
        ac, _ = client
        first = (await ac.get(_list(world["ada"]), params={"limit": 3})).json()
        second = (
            await ac.get(_list(world["ada"]), params={"limit": 3, "offset": 3})
        ).json()
        last = (
            await ac.get(_list(world["ada"]), params={"limit": 3, "offset": 6})
        ).json()
        assert [len(p["items"]) for p in (first, second, last)] == [3, 3, 1]
        assert first["total"] == second["total"] == last["total"] == 7
        ids = [i["id"] for p in (first, second, last) for i in p["items"]]
        assert len(set(ids)) == 7
        assert (second["limit"], second["offset"]) == (3, 3)

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
    async def test_the_paging_bounds(
        self, client: Client, world: dict[str, Any], params: dict[str, int]
    ) -> None:
        ac, _ = client
        assert (await ac.get(_list(world["ada"]), params=params)).status_code == 422

    async def test_a_page_costs_the_same_statements_whatever_its_size(
        self, client: Client, world: dict[str, Any], async_engine: AsyncEngine
    ) -> None:
        """Courses, tasks and the held marks are read for the page, not per row."""
        ac, _ = client
        statements: list[str] = []

        def _count(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            statements.append(statement)

        event.listen(async_engine.sync_engine, "before_cursor_execute", _count)
        try:
            await ac.get(_list(world["ada"]), params={"limit": 2})
            one = len(statements)
            statements.clear()
            await ac.get(_list(world["ada"]), params={"limit": 20})
            many = len(statements)
        finally:
            event.remove(async_engine.sync_engine, "before_cursor_execute", _count)
        assert one == many

    async def test_the_course_filter_narrows_the_page_and_the_total(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        c2 = (
            await ac.get(_list(world["ada"]), params={"course_node_id": world["c2"]})
        ).json()
        assert [i["id"] for i in c2["items"]] == [str(world["project"])]
        assert c2["total"] == 1
        c1 = (
            await ac.get(_list(world["ada"]), params={"course_node_id": world["c1"]})
        ).json()
        assert c1["total"] == 6
        assert {i["course_id"] for i in c1["items"]} == {str(world["c1"])}
        # The enrollments of the head are the student's, whatever the filter.
        assert len(c1["enrollments"]) == 2

    async def test_the_course_filter_does_not_reach_into_another_tenant(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        body = (
            await ac.get(
                _list(world["ada"]), params={"course_node_id": str(uuid.uuid4())}
            )
        ).json()
        assert (body["items"], body["total"]) == ([], 0)

    async def test_the_fields_of_a_row(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        items = {
            i["id"]: i for i in (await ac.get(_list(world["ada"]))).json()["items"]
        }
        a2 = items[str(world["a2"])]
        assert a2["attempt_no"] == 2
        assert a2["previous_submission_id"] == str(world["a1"])
        assert a2["course_title"] == "Course One"
        assert a2["task_document_id"] == str(world["task"])
        assert a2["task_title"] == "Sort a list"
        assert a2["task_type"] == "task"
        assert a2["status"] == "completed"
        assert a2["score"] == 90
        assert a2["passed"] is True
        assert a2["original_filename"] == "work.py"
        assert a2["is_repeat_copy"] is False
        assert a2["review_language"] == "ukr"
        # A task with neither title nor file name gets the label the portal gives.
        assert items[str(world["project"])]["task_title"] == "text #7"
        assert items[str(world["project"])]["task_type"] == "project"

    async def test_a_copy_of_an_earlier_review_is_marked(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        items = {
            i["id"]: i for i in (await ac.get(_list(world["ada"]))).json()["items"]
        }
        assert items[str(world["a3"])]["is_repeat_copy"] is True
        assert [k for k, v in items.items() if v["is_repeat_copy"]] == [
            str(world["a3"])
        ]

    async def test_the_language_of_the_review_else_the_one_requested(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        items = {
            i["id"]: i for i in (await ac.get(_list(world["ada"]))).json()["items"]
        }
        # Own language of a version-1 review wins over the one requested.
        assert items[str(world["a1"])]["review_language"] == "ukr"
        # An old review has none of its own: the requested language stands.
        assert items[str(world["old"])]["review_language"] == "eng"
        assert items[str(world["rejected"])]["review_language"] is None

    async def test_reviewed_at_is_the_end_of_the_job_that_wrote_the_review(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        items = {
            i["id"]: i for i in (await ac.get(_list(world["ada"]))).json()["items"]
        }
        expected = {
            "a1": _T0 + timedelta(days=1, minutes=3),
            "a2": _T0 + timedelta(days=2, minutes=3),
            "a3": _T0 + timedelta(days=3, minutes=3),
            "project": _T0 + timedelta(days=6),
        }
        for name, moment in expected.items():
            got = datetime.fromisoformat(items[str(world[name])]["reviewed_at"])
            assert got == moment, name
        # No job; a job that ended on a refused work; a test — no review time.
        for name in ("old", "rejected", "quiz"):
            assert items[str(world[name])]["reviewed_at"] is None, name

    async def test_a_refusal_carries_its_code_and_what_was_not_opened(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        items = {
            i["id"]: i for i in (await ac.get(_list(world["ada"]))).json()["items"]
        }
        rejected = items[str(world["rejected"])]
        assert rejected["presentation"] == {
            "state": "not_opened",
            "reason_code": "unsupported_extension",
        }
        assert rejected["rejection"]["code"] == "unsupported_extension"
        assert rejected["not_opened"] == [
            {"path": "a.exe", "reason": "magic", "size": 3}
        ]
        assert rejected["passed"] is None

    async def test_passed_and_presentation_are_the_portals_for_the_same_submission(
        self,
        client: Client,
        world: dict[str, Any],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ac, _ = client
        items = {
            i["id"]: i for i in (await ac.get(_list(world["ada"]))).json()["items"]
        }
        async with session_factory() as s:
            for name in ("a1", "a2", "old", "rejected", "project", "quiz"):
                row = await s.get(HomeworkSubmission, world[name])
                assert row is not None
                verdict = curated_verdict(
                    row.review_result, score=row.score, submission_id=row.id
                )
                item = items[str(row.id)]
                assert item["passed"] == (verdict.passed if verdict else None), name
                assert item["presentation"] == curated_presentation(row).model_dump(), (
                    name
                )


# ── The detail ──────────────────────────────────────────────────────────────


class TestDetail:
    async def test_the_keys_are_the_white_list(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        for name in ("a1", "a2", "a3", "old", "rejected", "project", "quiz"):
            body = (await ac.get(_detail(world["ada"], world[name]))).json()
            assert set(body) == DETAIL_KEYS, name
            assert set(body["student"]) == DETAIL_STUDENT_KEYS
            if body["pass_rule"] is not None:
                assert set(body["pass_rule"]) == PASS_RULE_KEYS
            assert all(set(c) == CRITERION_KEYS for c in body["criteria"])

    async def test_no_trace_reaches_the_author(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        for name in ("old", "rejected"):
            resp = await ac.get(_detail(world["ada"], world[name]))
            assert resp.status_code == 200
            assert _TRACE_MARKER not in resp.text, name
        listing = await ac.get(_list(world["ada"]))
        assert _TRACE_MARKER not in listing.text

    async def test_a_review_of_version_1_has_its_structure_and_its_criteria(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        body = (await ac.get(_detail(world["ada"], world["a1"]))).json()
        assert body["structure"]["schema_version"] == REVIEW_SCHEMA_VERSION
        assert body["structure"]["verdict"]["passed"] is False
        assert body["verdict"]["passed"] is False
        assert body["review_markdown"] == "# Review one"
        assert body["student"] == {
            "student_id": str(world["ada"]),
            "display_name": "Ada",
        }
        assert [c["item_id"] for c in body["criteria"]] == [
            "c1",
            "c2",
            "c2.p1",
            "c2.p2",
            "c3",
        ]
        c1, c2, p1, p2, c3 = body["criteria"]
        assert c1 == {
            "item_id": "c1",
            "item_kind": "criterion",
            "text": "Handles an empty list",
            "weight": "must",
            "must_by_author": True,
            "verdict": "not_met",
            "quote": None,
            "quote_line_start": None,
            "missing": "Nothing handles [].",
            "remark_state": "new",
        }
        assert (c2["text"], c2["must_by_author"], c2["weight"]) == (
            "Explains the idea",
            False,
            "should",
        )
        assert (p1["item_kind"], p1["text"], p1["weight"], p1["must_by_author"]) == (
            "point",
            "Names the algorithm",
            None,
            False,
        )
        assert (p2["verdict"], p2["missing"]) == ("not_met", "No cost given.")
        # A "met" no line of the work bore out is not "not met".
        assert (c3["verdict"], c3["quote"]) == ("unconfirmed", "def f(x) -> int")

    async def test_the_remark_state_moves_between_two_attempts(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        first = (await ac.get(_detail(world["ada"], world["a1"]))).json()
        second = (await ac.get(_detail(world["ada"], world["a2"]))).json()
        assert first["criteria"][0]["remark_state"] == "new"
        assert second["criteria"][0]["remark_state"] == "fixed"
        assert second["previous_submission_id"] == str(world["a1"])
        assert second["criteria"][0]["verdict"] == "met"
        assert second["criteria"][0]["quote_line_start"] == 4

    async def test_the_pass_rule_is_the_one_recorded_with_the_review(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        failing = (await ac.get(_detail(world["ada"], world["a1"]))).json()
        assert failing["pass_rule"] == {
            "basis": "author_musts_unmet",
            "threshold": 70,
            "required_musts": 1,
        }
        no_musts = (await ac.get(_detail(world["ada"], world["project"]))).json()
        assert no_musts["pass_rule"] == {
            "basis": "no_author_musts_passed",
            "threshold": 80,
            "required_musts": 0,
        }

    async def test_an_old_review_has_no_structure_no_criteria_and_no_rule(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        body = (await ac.get(_detail(world["ada"], world["old"]))).json()
        assert body["structure"] is None
        assert body["criteria"] == []
        assert body["pass_rule"] is None
        assert body["verdict"] == {"passed": True, "correctness": "correct"}
        assert body["review_markdown"] == "# Old review"

    async def test_a_test_and_a_refused_work_have_nothing_to_judge_by(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        quiz = (await ac.get(_detail(world["ada"], world["quiz"]))).json()
        assert quiz["criteria"] == []
        assert quiz["pass_rule"] is None
        assert quiz["structure"]["test"]["score"] == 50
        rejected = (await ac.get(_detail(world["ada"], world["rejected"]))).json()
        assert rejected["criteria"] == []
        assert rejected["structure"] is None
        assert rejected["rejection"]["code"] == "unsupported_extension"

    async def test_a_copy_is_marked_and_keeps_its_review(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        body = (await ac.get(_detail(world["ada"], world["a3"]))).json()
        assert body["is_repeat_copy"] is True
        assert body["structure"]["unchanged_from_attempt"] == 2
        assert body["attempt_no"] == 3

    async def test_a_project_has_its_delta(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        body = (await ac.get(_detail(world["ada"], world["project"]))).json()
        assert body["delta"] == {
            "changed": 1,
            "new": 1,
            "deleted": 1,
            "base_version": 1,
            "latest_version": 1,
            "is_stale": False,
        }
        assert body["task_type"] == "project"
        assert body["course_title"] == "Course Two"
        other = (await ac.get(_detail(world["ada"], world["a1"]))).json()
        assert other["delta"] is None

    async def test_the_students_own_answer_about_the_review(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        answered = (await ac.get(_detail(world["ada"], world["a2"]))).json()
        assert answered["own_feedback"]["kind"] == "touch"
        assert answered["own_feedback"]["value"] == "helped"
        silent = (await ac.get(_detail(world["ada"], world["a1"]))).json()
        assert silent["own_feedback"] is None

    async def test_the_detail_row_fields_match_the_list_row(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        listed = {
            i["id"]: i for i in (await ac.get(_list(world["ada"]))).json()["items"]
        }
        for name in ("a1", "a2", "a3", "old", "rejected", "project", "quiz"):
            detail = (await ac.get(_detail(world["ada"], world[name]))).json()
            row = listed[str(world[name])]
            for key in _BASE_KEYS:
                assert detail[key] == row[key], (name, key)

    async def test_an_unreadable_list_gives_no_criteria_and_a_warning_of_ids_only(
        self,
        client: Client,
        world: dict[str, Any],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ac, _ = client
        async with session_factory() as s:
            await s.execute(
                delete(TaskCriteriaOverride).where(
                    TaskCriteriaOverride.id == world["override"]
                )
            )
            await s.commit()
        with capture_logs() as logs:
            resp = await ac.get(_detail(world["ada"], world["a1"]))
        assert resp.status_code == 200
        body = resp.json()
        assert body["criteria"] == []
        # The rule is the one recorded; only the count of musts is unknown.
        assert body["pass_rule"]["basis"] == "author_musts_unmet"
        assert body["pass_rule"]["required_musts"] is None
        (warning,) = [
            e for e in logs if e["event"] == "author_submission_judged_list_unreadable"
        ]
        assert warning["log_level"] == "warning"
        assert set(warning) == {"event", "log_level", "tenant_id", "submission_id"}
        assert "Handles an empty list" not in str(logs)


# ── The file ────────────────────────────────────────────────────────────────


class TestFile:
    async def test_a_link_for_fifteen_minutes_as_an_attachment(
        self, client: Client, world: dict[str, Any], s3: _FakeS3
    ) -> None:
        ac, _ = client
        before = datetime.now(UTC)
        resp = await ac.get(_file(world["ada"], world["a1"]))
        after = datetime.now(UTC)
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == FILE_LINK_KEYS
        assert (
            body["url"] == f"https://signed.example/homework/work.py?sig={_URL_MARKER}"
        )
        expires = datetime.fromisoformat(body["expires_at"])
        assert (
            before + timedelta(seconds=FILE_LINK_TTL_SEC)
            <= expires
            <= after + timedelta(seconds=FILE_LINK_TTL_SEC)
        )
        assert FILE_LINK_TTL_SEC == 15 * 60
        (call,) = s3.presigned
        assert call["key"] == "homework/work.py"
        assert call["expires_in"] == FILE_LINK_TTL_SEC
        assert call["content_disposition"].startswith("attachment;")
        assert 'filename="work.py"' in call["content_disposition"]

    async def test_a_name_that_could_break_the_header_cannot(
        self,
        client: Client,
        world: dict[str, Any],
        s3: _FakeS3,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ac, _ = client
        async with session_factory() as s:
            row = await s.get(HomeworkSubmission, world["a1"])
            assert row is not None
            row.original_filename = 'ро"зв;\r\nX-Evil: 1.py'
            await s.commit()
        assert (await ac.get(_file(world["ada"], world["a1"]))).status_code == 200
        header = s3.presigned[0]["content_disposition"]
        assert "\r" not in header
        assert "\n" not in header
        assert header.startswith('attachment; filename="')
        assert "filename*=UTF-8''" in header

    async def test_a_test_has_no_file(
        self, client: Client, world: dict[str, Any], s3: _FakeS3
    ) -> None:
        ac, _ = client
        resp = await ac.get(_file(world["ada"], world["quiz"]))
        assert resp.status_code == 404
        assert resp.json() == {"detail": SUBMISSION_NOT_FOUND}
        assert s3.presigned == []

    async def test_a_file_outside_the_bucket_has_no_link(
        self,
        client: Client,
        world: dict[str, Any],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ac, _ = client
        async with session_factory() as s:
            row = await s.get(HomeworkSubmission, world["a1"])
            assert row is not None
            row.file_url = "https://elsewhere.example/work.py"
            await s.commit()
        resp = await ac.get(_file(world["ada"], world["a1"]))
        assert resp.status_code == 404

    async def test_the_log_names_who_and_which_and_never_the_link_or_the_key(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        with capture_logs() as logs:
            resp = await ac.get(_file(world["ada"], world["a1"]))
        assert resp.status_code == 200
        (line,) = [e for e in logs if e["event"] == "author_submission_file_link"]
        assert line["tenant_id"] == str(world["tenant"])
        assert line["student_id"] == str(world["ada"])
        assert line["submission_id"] == str(world["a1"])
        assert set(line) == {
            "event",
            "log_level",
            "tenant_id",
            "student_id",
            "submission_id",
        }
        for entry in logs:
            text = str(entry)
            assert _URL_MARKER not in text
            assert "homework/work.py" not in text
            assert "signed.example" not in text


# ── The talk under the review ───────────────────────────────────────────────

THREADS_KEYS = {"accepting_replies", "threads"}
THREAD_KEYS = {"item_id", "comments"}
COMMENT_KEYS = {
    "id",
    "submission_id",
    "item_id",
    "parent_id",
    "author",
    "kind",
    "body",
    "created_at",
    "answered_in_submission_id",
}


@pytest.fixture()
def replies_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped file with replies on for ``task``, where the app reads it."""
    config = shipped_with_replies_for(AssignmentType.TASK)
    monkeypatch.setattr(path_config, "_config", config)


class TestComments:
    async def test_the_author_reads_what_the_student_reads_in_the_portal(
        self, client: Client, world: dict[str, Any], replies_on: None
    ) -> None:
        ac, _ = client
        app.dependency_overrides[get_current_student] = lambda: StudentContext(
            student_id=world["ada"],
            tenant_id=world["tenant"],
            login="ada-login",
            display_name="Ada",
        )
        for name in ("a1", "a2", "a3"):
            theirs = await ac.get(f"/api/v1/portal/submissions/{world[name]}/comments")
            ours = await ac.get(_comments(world["ada"], world[name]))
            assert theirs.status_code == ours.status_code == 200, name
            assert ours.json() == theirs.json(), name
        a2 = (await ac.get(_comments(world["ada"], world["a2"]))).json()
        # Two threads, in the order of the review's remarks: the question with
        # the Mentor's answer under it, then the objection nobody answered yet.
        assert [t["item_id"] for t in a2["threads"]] == ["c1", "c2"]
        first, second = a2["threads"]
        assert [(c["author"], c["kind"]) for c in first["comments"]] == [
            ("student", "question"),
            ("mentor", "answer"),
        ]
        assert first["comments"][1]["parent_id"] == first["comments"][0]["id"]
        assert [(c["author"], c["kind"]) for c in second["comments"]] == [
            ("student", "objection")
        ]
        assert a2["accepting_replies"] is False  # a3 came after it

    async def test_the_keys_are_the_white_list(
        self, client: Client, world: dict[str, Any], replies_on: None
    ) -> None:
        ac, _ = client
        body = (await ac.get(_comments(world["ada"], world["a2"]))).json()
        assert set(body) == THREADS_KEYS
        assert body["threads"]
        for thread in body["threads"]:
            assert set(thread) == THREAD_KEYS
            assert all(set(c) == COMMENT_KEYS for c in thread["comments"])

    async def test_a_type_that_takes_no_replies_has_empty_threads(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        # The shipped file takes no replies: the same empty answer as the portal.
        ac, _ = client
        body = (await ac.get(_comments(world["ada"], world["a2"]))).json()
        assert body == {"accepting_replies": False, "threads": []}

    async def test_a_review_nobody_commented_on_has_empty_threads(
        self, client: Client, world: dict[str, Any], replies_on: None
    ) -> None:
        ac, _ = client
        for name in ("old", "rejected", "quiz"):
            body = (await ac.get(_comments(world["ada"], world[name]))).json()
            assert body["threads"] == [], name
            assert set(body) == THREADS_KEYS

    async def test_the_route_only_reads(
        self, client: Client, world: dict[str, Any]
    ) -> None:
        ac, _ = client
        resp = await ac.post(
            _comments(world["ada"], world["a1"]),
            json={"item_id": "c1", "kind": "question", "body": "x"},
        )
        assert resp.status_code == 405
