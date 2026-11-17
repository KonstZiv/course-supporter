"""Comments on remarks and the state of a remark, as the database holds them.

Mentor-rebuild task 12a, K1: ``remark_comments`` and
``submission_criterion_verdicts.remark_state``. Requires ``docker compose up
-d`` (PostgreSQL), migrated to head; run with ``--run-db``.

What only a live database answers:

* the migration is the ORM: every column (type, NULL, default, comment), every
  CHECK, index and foreign key, and the table's comment, compared with the
  table the ORM itself creates — both as PostgreSQL renders them
  (``test_schema_sync`` compares names only, ``DD-L4-A``);
* each CHECK refuses its own row, and for its own reason only: with that one
  rule dropped (inside the test's transaction) the same row is admitted. A rule
  held by the database is proven by changing the schema, not the code;
* every member of every vocabulary is admitted;
* a delete reaches the comments as it reaches a student's other data — with
  the student, the tenant and the submission, a reply with its parent; the
  delete of the answering attempt leaves the comment unanswered; the task
  stays while a comment names it;
* the migration goes down and up.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import (
    Column,
    ForeignKeyConstraint,
    MetaData,
    Table,
    create_engine,
    insert,
    inspect,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.config import get_settings
from course_supporter.criteria_kinds import VerdictValue
from course_supporter.feedback_kinds import (
    FeedbackKind,
    FeedbackTargetKind,
    FeedbackValue,
)
from course_supporter.remark_kinds import (
    COMMENT_MAX_CHARS,
    CommentAuthor,
    CommentKind,
    CommentTopic,
    RemarkState,
)
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    RemarkComment,
    StudentFeedback,
    SubmissionCriterionVerdict,
    Tenant,
)
from course_supporter.storage.student_repository import StudentRepository

pytestmark = pytest.mark.requires_db

REVISION = "task12a_remark_comments"
BELOW = "esc_request_traces"

_COMMENTS = cast(Table, RemarkComment.__table__)
_VERDICTS = cast(Table, SubmissionCriterionVerdict.__table__)
_STATE_CHECK = "ck_submission_criterion_verdicts_remark_state"
_MIRROR = "orm_mirror_task12a"
"""A scratch schema for the tables the ORM creates; it lives in one test's
transaction and goes with its rollback."""

_FAIL_FAST = text("SET LOCAL lock_timeout = '10s'")
"""The DDL of a test waits for a lock no longer than this: a session left open
elsewhere fails the test instead of hanging the run."""

_TASK_VERSION = "3f5a0c9e"
_THIS_SUBMISSION = "<this submission>"
"""Stands in a parametrized row for the id of the test's own submission."""

_CREDITED_NOW: dict[RemarkState, bool] = {
    RemarkState.NEW: False,
    RemarkState.OPEN: False,
    RemarkState.FIXED: True,
    RemarkState.BROKEN: False,
}
"""Whether each state's item is credited now, by the one table of task 12a:
"fixed" — credited, the other three — not. The tests below write the plain row
of each — credited as "met", not credited as "not_met"; the database judges
neither (the rule of what is credited is the code's)."""


# ── The rows ────────────────────────────────────────────────────────────────


@pytest.fixture()
async def attempts(
    db_session: AsyncSession,
    seed_tenant: Tenant,
    seed_root_node: CourseNode,
    seed_material_entry: AuthoredDocument,
) -> tuple[HomeworkSubmission, HomeworkSubmission]:
    """Two attempts of one student at one task: the first, and the next one."""
    student = await StudentRepository(db_session).create(
        tenant_id=seed_tenant.id, external_id=f"ext-{uuid.uuid4().hex[:8]}"
    )
    repository = HomeworkRepository(db_session)
    first, second = [
        await repository.create(
            tenant_id=seed_tenant.id,
            student_id=student.id,
            course_node_id=seed_root_node.id,
            node_id=seed_root_node.id,
            authored_document_id=seed_material_entry.id,
            file_url="s3://bucket/solution.py",
            file_type="text/plain",
            original_filename="solution.py",
        )
        for _ in range(2)
    ]
    return first, second


@pytest.fixture()
def submission(
    attempts: tuple[HomeworkSubmission, HomeworkSubmission],
) -> HomeworkSubmission:
    return attempts[0]


def _students(submission: HomeworkSubmission, **overrides: Any) -> dict[str, Any]:
    """A row every CHECK admits: the student's question on a remark, judged."""
    row: dict[str, Any] = {
        "tenant_id": submission.tenant_id,
        "student_id": submission.student_id,
        "authored_document_id": submission.authored_document_id,
        "task_version": _TASK_VERSION,
        "submission_id": submission.id,
        "item_id": "c1",
        "parent_id": None,
        "author": CommentAuthor.STUDENT.value,
        "kind": CommentKind.QUESTION.value,
        "body": "Чому цей пункт не зараховано?",
        "topic": CommentTopic.ON_TOPIC.value,
        "topic_confidence": 0.9,
        "hostile_category": None,
        "answered_in_submission_id": None,
    }
    row.update(overrides)
    return {
        key: submission.id if value == _THIS_SUBMISSION else value
        for key, value in row.items()
    }


def _mentors(submission: HomeworkSubmission, **overrides: Any) -> dict[str, Any]:
    """A row every CHECK admits: the Mentor's answer, which has no topic."""
    row: dict[str, Any] = {
        "author": CommentAuthor.MENTOR.value,
        "kind": CommentKind.ANSWER.value,
        "body": "Бо функція не перевіряє порожній список.",
        "topic": None,
        "topic_confidence": None,
    }
    row.update(overrides)
    return _students(submission, **row)


async def _comment(session: AsyncSession, row: dict[str, Any]) -> uuid.UUID:
    written = await session.execute(
        insert(_COMMENTS).values(**row).returning(_COMMENTS.c.id)
    )
    return cast(uuid.UUID, written.scalar_one())


def _verdict(submission: HomeworkSubmission, **overrides: Any) -> dict[str, Any]:
    """A verdict row every CHECK admits: a criterion met, with no state."""
    row: dict[str, Any] = {
        "tenant_id": submission.tenant_id,
        "submission_id": submission.id,
        "criteria_layer": "model",
        "criteria_source_id": uuid.uuid4(),
        "item_id": "c1",
        "item_kind": "criterion",
        "weight": "must",
        "verdict": VerdictValue.MET.value,
        "model_verdict": VerdictValue.MET.value,
        "quote": "def total(items):",
        "quote_file": "solution.py",
        "quote_line_start": 3,
        "quote_line_end": 3,
        "remark_state": None,
    }
    row.update(overrides)
    return row


def _verdict_on(
    submission: HomeworkSubmission, verdict: VerdictValue, **overrides: Any
) -> dict[str, Any]:
    """A verdict row every CHECK admits, with the verdict asked for."""
    if verdict is VerdictValue.MET:
        return _verdict(submission, **overrides)
    unmet: dict[str, Any] = {
        "verdict": VerdictValue.NOT_MET.value,
        "model_verdict": VerdictValue.NOT_MET.value,
        "quote": None,
        "quote_file": None,
        "quote_line_start": None,
        "quote_line_end": None,
        "missing": "Немає перевірки порожнього списку.",
    }
    unmet.update(overrides)
    return _verdict(submission, **unmet)


async def _verdict_row(session: AsyncSession, row: dict[str, Any]) -> None:
    await session.execute(insert(_VERDICTS).values(**row))


async def _refused_by_it_alone(
    session: AsyncSession,
    table: str,
    constraint: str,
    write: Callable[[], Awaitable[object]],
) -> None:
    """The row is refused by ``constraint``, and by nothing else.

    PostgreSQL tests the CHECKs in the order of their names and names only the
    first that fails, so the refusal alone does not say the row breaks no other
    rule. The second half does: the same row, with that one rule dropped, is
    admitted. The drop is undone with the test's transaction.
    """
    with pytest.raises(IntegrityError, match=f'"{constraint}"'):
        async with session.begin_nested():
            await write()
    await session.execute(_FAIL_FAST)
    await session.execute(text(f'ALTER TABLE {table} DROP CONSTRAINT "{constraint}"'))
    await write()


# ── The migration is the ORM ────────────────────────────────────────────────


async def _create_orm_mirror(session: AsyncSession, *tables: Table) -> None:
    """Create ``tables`` as the ORM declares them, in the scratch schema.

    A foreign key to a table outside the mirror points at the live one: those
    tables enter the metadata as bare references, never created. The default
    convention names the index of an ``index=True`` column after the column's
    label, which carries the schema; for a table without one, as in the ORM,
    that is the name below.
    """
    mirrored = {table.name for table in tables}
    metadata = MetaData(naming_convention={"ix": "ix_%(table_name)s_%(column_0_name)s"})
    for table in tables:
        for foreign_key in table.foreign_keys:
            target = foreign_key.column
            if target.table.name in mirrored | set(metadata.tables):
                continue
            Table(
                target.table.name,
                metadata,
                Column(target.name, target.type, primary_key=True),
            )

    def referred_schema(
        _table: Table,
        to_schema: str | None,
        constraint: ForeignKeyConstraint,
        _referred: str | None,
    ) -> str | None:
        return to_schema if constraint.referred_table.name in mirrored else None

    copies = [
        table.to_metadata(metadata, schema=_MIRROR, referred_schema_fn=referred_schema)
        for table in tables
    ]
    await session.execute(_FAIL_FAST)
    await session.execute(text(f"CREATE SCHEMA {_MIRROR}"))
    connection = await session.connection()
    await connection.run_sync(
        lambda sync: metadata.create_all(sync, tables=copies, checkfirst=False)
    )


async def _shape(session: AsyncSession, schema: str, table: str) -> dict[str, Any]:
    """A table as PostgreSQL renders it, with its schema's name taken out."""
    qualified = {"t": f"{schema}.{table}"}

    def local(definition: str) -> str:
        return definition.replace(f"{schema}.", "")

    columns = await session.execute(
        text(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
            "pg_get_expr(d.adbin, d.adrelid), col_description(a.attrelid, a.attnum) "
            "FROM pg_attribute a LEFT JOIN pg_attrdef d "
            "ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST(:t AS regclass) "
            "AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        qualified,
    )
    constraints = await session.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = CAST(:t AS regclass)"
        ),
        qualified,
    )
    indexes = await session.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = :schema AND tablename = :table"
        ),
        {"schema": schema, "table": table},
    )
    comment = await session.scalar(
        text("SELECT obj_description(CAST(:t AS regclass), 'pg_class')"), qualified
    )
    return {
        "columns": {name: tuple(rest) for name, *rest in columns.all()},
        "constraints": {name: local(sql) for name, sql in constraints.all()},
        "indexes": {name: local(sql) for name, sql in indexes.all()},
        "comment": comment,
    }


class TestTheMigrationIsTheOrm:
    async def test_the_comments_table(self, db_session: AsyncSession) -> None:
        live_schema = await db_session.scalar(text("SELECT current_schema()"))
        await _create_orm_mirror(db_session, _COMMENTS)

        live = await _shape(db_session, str(live_schema), _COMMENTS.name)
        orm = await _shape(db_session, _MIRROR, _COMMENTS.name)

        # The premise: the live table has exactly the ORM's columns, each with
        # a comment, and the comparison below walks every one of them.
        assert set(live["columns"]) == {column.name for column in _COMMENTS.columns}
        assert all(column.comment for column in _COMMENTS.columns)
        assert live == orm

    async def test_the_state_column_and_its_rule(
        self, db_session: AsyncSession
    ) -> None:
        live_schema = await db_session.scalar(text("SELECT current_schema()"))
        await _create_orm_mirror(db_session, _VERDICTS)

        live = await _shape(db_session, str(live_schema), _VERDICTS.name)
        orm = await _shape(db_session, _MIRROR, _VERDICTS.name)

        assert live["columns"]["remark_state"] == orm["columns"]["remark_state"]
        assert live["constraints"][_STATE_CHECK] == orm["constraints"][_STATE_CHECK]

    async def test_the_new_rows_are_written_with_the_orm(
        self, db_session: AsyncSession, attempts: tuple[HomeworkSubmission, ...]
    ) -> None:
        """The ORM's own write path: a comment and a stated verdict, as K2 and
        K3 will write them."""
        first, second = attempts
        question = RemarkComment(**_students(first))
        db_session.add(question)
        await db_session.flush()
        db_session.add_all(
            [
                RemarkComment(**_mentors(second, parent_id=question.id)),
                SubmissionCriterionVerdict(
                    **_verdict_on(second, VerdictValue.MET, remark_state="fixed")
                ),
            ]
        )
        await db_session.flush()

        assert question.created_at is not None


# ── The rules of a comment ──────────────────────────────────────────────────


class TestTheDatabaseHoldsTheCommentRules:
    async def test_a_branch_is_written(
        self, db_session: AsyncSession, attempts: tuple[HomeworkSubmission, ...]
    ) -> None:
        """The premise of every refusal below: the base rows are admitted — a
        question on a point, the Mentor's answer in the next review, an
        objection to it not judged yet."""
        first, second = attempts
        question = await _comment(
            db_session,
            _students(first, item_id="c2.p1", answered_in_submission_id=second.id),
        )
        answer = await _comment(
            db_session, _mentors(second, item_id="c2.p1", parent_id=question)
        )
        await _comment(
            db_session,
            _students(
                second,
                item_id="c2.p1",
                parent_id=answer,
                kind=CommentKind.OBJECTION.value,
                topic=None,
                topic_confidence=None,
            ),
        )

    async def test_every_member_of_every_vocabulary_is_admitted(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        rows = [
            _students(submission, kind=kind.value, topic=topic.value)
            for kind in CommentKind
            if kind is not CommentKind.ANSWER
            for topic in CommentTopic
        ]
        rows += [_mentors(submission, kind=kind.value) for kind in CommentKind]
        for row in rows:
            await _comment(db_session, row)

        assert len(rows) == (len(CommentKind) - 1) * len(CommentTopic) + len(
            CommentKind
        )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"body": "ї" * COMMENT_MAX_CHARS},
            {"topic_confidence": 0.0},
            {"topic_confidence": 1.0},
            {"topic": None, "topic_confidence": None},
            {"topic": "hostile", "hostile_category": "insult"},
            {"answered_in_submission_id": _THIS_SUBMISSION},
            {"item_id": "c12.p3"},
        ],
        ids=lambda overrides: ",".join(overrides),
    )
    async def test_a_row_at_the_edge_of_a_rule_is_admitted(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        overrides: dict[str, Any],
    ) -> None:
        """The limit of the text is in characters: 4000 two-byte letters fit."""
        await _comment(db_session, _students(submission, **overrides))

    @pytest.mark.parametrize(
        ("constraint", "overrides"),
        [
            ("author", {"author": "teacher"}),
            ("kind", {"kind": "remark"}),
            ("answer_by_mentor", {"kind": "answer"}),
            ("body_not_empty", {"body": ""}),
            ("body_not_empty", {"body": " \n\t "}),
            ("body_length", {"body": "ї" * (COMMENT_MAX_CHARS + 1)}),
            ("topic", {"topic": "unclear"}),
            ("topic_confidence", {"topic_confidence": 1.01}),
            ("topic_confidence", {"topic_confidence": -0.01}),
            ("topic_confidence", {"topic_confidence": float("nan")}),
            (
                "topic_of_student",
                {"author": "mentor", "kind": "answer"},
            ),
            (
                "topic_of_student",
                {
                    "author": "mentor",
                    "kind": "answer",
                    "topic": None,
                    "topic_confidence": None,
                    "hostile_category": "insult",
                },
            ),
            (
                "answered_of_student",
                {
                    "author": "mentor",
                    "kind": "answer",
                    "topic": None,
                    "topic_confidence": None,
                    "answered_in_submission_id": _THIS_SUBMISSION,
                },
            ),
            ("item_id_shape", {"item_id": "c0"}),
            ("item_id_shape", {"item_id": "c1.p0"}),
            ("item_id_shape", {"item_id": "C1"}),
            ("item_id_shape", {"item_id": "c1.p1.x"}),
            ("item_id_shape", {"item_id": ""}),
        ],
    )
    async def test_a_row_breaking_a_rule_is_refused_by_that_rule_alone(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        constraint: str,
        overrides: dict[str, Any],
    ) -> None:
        row = _students(submission, **overrides)

        await _refused_by_it_alone(
            db_session,
            _COMMENTS.name,
            f"ck_remark_comments_{constraint}",
            lambda: _comment(db_session, row),
        )


# ── The rules of a state ────────────────────────────────────────────────────


class TestTheDatabaseHoldsTheStateRules:
    def test_every_state_is_credited_or_not_now(self) -> None:
        """The premise of the test below: it walks every state."""
        assert set(_CREDITED_NOW) == set(RemarkState)

    async def test_every_state_is_admitted(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        for number, (state, credited) in enumerate(_CREDITED_NOW.items(), start=1):
            now = VerdictValue.MET if credited else VerdictValue.NOT_MET
            await _verdict_row(
                db_session,
                _verdict_on(
                    submission, now, item_id=f"c{number}", remark_state=state.value
                ),
            )

    async def test_an_unconfirmed_item_credited_now_is_admitted_as_fixed(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        """Credited as the score counts it: "not_met" with quote_not_found.
        An item not credited before is "fixed" on such a row, and the database
        does not refuse it."""
        unconfirmed = _verdict(
            submission,
            verdict=VerdictValue.NOT_MET.value,
            model_verdict=VerdictValue.MET.value,
            quote_file=None,
            quote_line_start=None,
            quote_line_end=None,
            quote_not_found=True,
            remark_state=RemarkState.FIXED.value,
        )

        await _verdict_row(db_session, unconfirmed)

    @pytest.mark.parametrize("now", list(VerdictValue))
    async def test_no_state_is_admitted_on_either_verdict(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        now: VerdictValue,
    ) -> None:
        """Every row written before task 12a: nothing compared, no state."""
        await _verdict_row(db_session, _verdict_on(submission, now))

    @pytest.mark.parametrize("now", list(VerdictValue))
    async def test_an_unknown_state_is_refused_by_the_vocabulary_alone(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        now: VerdictValue,
    ) -> None:
        row = _verdict_on(submission, now, remark_state="closed")

        await _refused_by_it_alone(
            db_session,
            _VERDICTS.name,
            _STATE_CHECK,
            lambda: _verdict_row(db_session, row),
        )


# ── The deletes ─────────────────────────────────────────────────────────────


async def _branch(
    session: AsyncSession, first: HomeworkSubmission, second: HomeworkSubmission
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A branch over two attempts: the student's question on the first review,
    answered in the second; the Mentor's answer; the student's objection to it."""
    question = await _comment(
        session, _students(first, answered_in_submission_id=second.id)
    )
    answer = await _comment(session, _mentors(second, parent_id=question))
    objection = await _comment(
        session,
        _students(
            second,
            parent_id=answer,
            kind=CommentKind.OBJECTION.value,
            topic=None,
            topic_confidence=None,
        ),
    )
    return question, answer, objection


async def _left(
    session: AsyncSession, ids: tuple[uuid.UUID, ...]
) -> dict[uuid.UUID, uuid.UUID | None]:
    """The comments of ``ids`` still there, each with its answering attempt."""
    rows = await session.execute(
        text(
            "SELECT id, answered_in_submission_id FROM remark_comments "
            "WHERE id = ANY(:ids)"
        ),
        {"ids": list(ids)},
    )
    return {row.id: row.answered_in_submission_id for row in rows}


class TestTheDeletesReachTheComments:
    async def test_the_students_delete_takes_them_as_it_takes_their_feedback(
        self, db_session: AsyncSession, attempts: tuple[HomeworkSubmission, ...]
    ) -> None:
        first, second = attempts
        branch = await _branch(db_session, first, second)
        db_session.add(
            StudentFeedback(
                tenant_id=first.tenant_id,
                student_id=first.student_id,
                target_kind=FeedbackTargetKind.REVIEW.value,
                target_id=first.id,
                kind=FeedbackKind.TOUCH.value,
                value=FeedbackValue.HELPED.value,
            )
        )
        await db_session.flush()

        await db_session.execute(
            text("DELETE FROM students WHERE id = :id"), {"id": first.student_id}
        )

        assert await _left(db_session, branch) == {}
        feedback = await db_session.scalar(
            text("SELECT count(*) FROM student_feedback WHERE student_id = :id"),
            {"id": first.student_id},
        )
        assert feedback == 0

    async def test_the_tenants_delete_reaches_them_by_every_path(
        self, db_session: AsyncSession, attempts: tuple[HomeworkSubmission, ...]
    ) -> None:
        """One comment is reached by the tenant, the student, the submission and
        the parent — and by the answering attempt's SET NULL; the delete goes
        through (``action_cost_estimates`` has no keys for want of this)."""
        first, second = attempts
        branch = await _branch(db_session, first, second)

        await db_session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": first.tenant_id}
        )

        assert await _left(db_session, branch) == {}

    async def test_a_submissions_delete_takes_its_comments_and_the_replies(
        self, db_session: AsyncSession, attempts: tuple[HomeworkSubmission, ...]
    ) -> None:
        """The answer and the objection are on the second attempt, and go with
        the question they reply to."""
        first, second = attempts
        branch = await _branch(db_session, first, second)

        await db_session.execute(
            text("DELETE FROM homework_submissions WHERE id = :id"), {"id": first.id}
        )

        assert await _left(db_session, branch) == {}

    async def test_the_answering_attempts_delete_leaves_the_question_unanswered(
        self, db_session: AsyncSession, attempts: tuple[HomeworkSubmission, ...]
    ) -> None:
        first, second = attempts
        question, _answer, _objection = branch = await _branch(
            db_session, first, second
        )

        await db_session.execute(
            text("DELETE FROM homework_submissions WHERE id = :id"), {"id": second.id}
        )

        assert await _left(db_session, branch) == {question: None}

    async def test_the_task_stays_while_a_comment_names_it(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        seed_root_node: CourseNode,
    ) -> None:
        """RESTRICT, as on the submissions: here the comment alone names the
        task, so its own key is what refuses."""
        task = AuthoredDocument(
            course_node_id=seed_root_node.id,
            course_root_id=seed_root_node.id,
            source_type="web",
            source_url="https://example.com/another-task",
        )
        db_session.add(task)
        await db_session.flush()
        await _comment(db_session, _students(submission, authored_document_id=task.id))

        with pytest.raises(
            IntegrityError, match='"remark_comments_authored_document_id_fkey"'
        ):
            await db_session.execute(
                text("DELETE FROM authored_documents WHERE id = :id"), {"id": task.id}
            )


# ── The migration ───────────────────────────────────────────────────────────


def test_one_head_and_the_revision_follows_the_request_traces() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    revision = script.get_revision(REVISION)

    assert len(heads) == 1
    assert revision is not None
    assert revision.down_revision == BELOW
    assert REVISION in {rev.revision for rev in script.walk_revisions("base", heads[0])}


def test_the_migration_goes_down_and_up() -> None:
    cfg = Config("alembic.ini")
    head = ScriptDirectory.from_config(cfg).get_current_head()
    engine = create_engine(get_settings().database_url)
    states = text(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = CAST(:t AS regclass) AND conname LIKE '%remark_state%'"
    )
    try:
        command.downgrade(cfg, BELOW)
        with engine.connect() as conn:
            assert _COMMENTS.name not in inspect(conn).get_table_names()
            columns = {c["name"] for c in inspect(conn).get_columns(_VERDICTS.name)}
            assert "remark_state" not in columns
            assert conn.execute(states, {"t": _VERDICTS.name}).all() == []
    finally:
        command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == head
        assert _COMMENTS.name in inspect(conn).get_table_names()
        names = {row.conname for row in conn.execute(states, {"t": _VERDICTS.name})}
        assert names == {_STATE_CHECK}
    engine.dispose()
