"""The two tables of task 09b, as the database holds them (task 09b, K1).

Requires ``docker compose up -d`` (PostgreSQL), migrated to head.

Two things only a live database answers:

* the comments the migration copied by hand are the ORM's — ``test_schema_sync``
  compares tables and columns, not comments (``DD-L4-A``);
* the rules a verdict row must keep are the DATABASE's, not only the writer's:
  each ``CHECK`` refuses the one row that breaks it, and admits every member of
  the vocabulary it is built from. A rule held by the database cannot be proven
  by mutating the code — only a mutation of the schema turns these red.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from sqlalchemy import Table, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    SubmissionCriterionVerdict,
    SubmissionExplanation,
    Tenant,
)
from course_supporter.storage.student_repository import StudentRepository

pytestmark = pytest.mark.requires_db

_TASK_09B_TABLES = [
    cast(Table, SubmissionCriterionVerdict.__table__),
    cast(Table, SubmissionExplanation.__table__),
]


async def _column_comments(
    session: AsyncSession, table_name: str
) -> dict[str, str | None]:
    result = await session.execute(
        text(
            "SELECT a.attname, col_description(a.attrelid, a.attnum) "
            "FROM pg_attribute a "
            "WHERE a.attrelid = CAST(:table AS regclass) "
            "AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"table": table_name},
    )
    return {str(name): comment for name, comment in result.all()}


class TestCommentsAreTheOrms:
    @pytest.mark.parametrize("table", _TASK_09B_TABLES, ids=lambda t: t.name)
    async def test_every_column_comment_is_the_orms(
        self, db_session: AsyncSession, table: Table
    ) -> None:
        live = await _column_comments(db_session, table.name)

        # The premise: the live table has exactly the ORM's columns, so the
        # comparison below walks every one of them.
        assert set(live) == {column.name for column in table.columns}
        for column in table.columns:
            assert live[column.name] == column.comment, column.name

    @pytest.mark.parametrize("table", _TASK_09B_TABLES, ids=lambda t: t.name)
    async def test_the_table_comment_is_the_orms(
        self, db_session: AsyncSession, table: Table
    ) -> None:
        live = await db_session.execute(
            text("SELECT obj_description(CAST(:table AS regclass), 'pg_class')"),
            {"table": table.name},
        )

        assert live.scalar_one() == table.comment


@pytest.fixture()
async def submission(
    db_session: AsyncSession,
    seed_tenant: Tenant,
    seed_root_node: CourseNode,
    seed_material_entry: AuthoredDocument,
) -> HomeworkSubmission:
    student = await StudentRepository(db_session).create(
        tenant_id=seed_tenant.id, external_id=f"ext-{uuid.uuid4().hex[:8]}"
    )
    return await HomeworkRepository(db_session).create(
        tenant_id=seed_tenant.id,
        student_id=student.id,
        course_node_id=seed_root_node.id,
        node_id=seed_root_node.id,
        authored_document_id=seed_material_entry.id,
        file_url="s3://bucket/solution.py",
        file_type="text/plain",
        original_filename="solution.py",
    )


def _met_criterion(submission: HomeworkSubmission, **overrides: Any) -> dict[str, Any]:
    """A row every CHECK admits: a criterion the model judged met, with a place."""
    row: dict[str, Any] = {
        "tenant_id": submission.tenant_id,
        "submission_id": submission.id,
        "criteria_layer": CriteriaLayer.MODEL.value,
        "criteria_source_id": uuid.uuid4(),
        "item_id": "c1",
        "item_kind": VerdictItemKind.CRITERION.value,
        "weight": WeightCategory.MUST.value,
        "verdict": VerdictValue.MET.value,
        "model_verdict": VerdictValue.MET.value,
        "quote": "def total(items):",
        "quote_file": "solution.py",
        "quote_line_start": 3,
        "quote_line_end": 3,
        "missing": None,
        "retried": False,
        "quote_not_found": False,
        "safeguard_fired": False,
        "safeguard_submission_id": None,
    }
    row.update(overrides)
    return row


async def _write(session: AsyncSession, row: dict[str, Any]) -> None:
    session.add(SubmissionCriterionVerdict(**row))
    await session.flush()


class TestTheDatabaseHoldsTheVerdictRules:
    async def test_a_valid_row_is_written(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        """The premise of every refusal below: the base row itself is admitted."""
        await _write(db_session, _met_criterion(submission))

    async def test_every_member_of_every_vocabulary_is_admitted(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        """Each CHECK built from an enum admits each member — the two agree."""
        rows = [
            _met_criterion(
                submission,
                item_id=f"c{number}",
                criteria_layer=layer.value,
                weight=weight.value,
                verdict=verdict.value,
                model_verdict=verdict.value,
            )
            for number, (layer, weight, verdict) in enumerate(
                [
                    (layer, weight, verdict)
                    for layer in CriteriaLayer
                    for weight in WeightCategory
                    for verdict in VerdictValue
                ],
                start=1,
            )
        ]
        rows.append(
            _met_criterion(
                submission,
                item_id="c99.p1",
                item_kind=VerdictItemKind.POINT.value,
                weight=None,
            )
        )
        for row in rows:
            await _write(db_session, row)

        assert (
            len(rows)
            == len(CriteriaLayer) * len(WeightCategory) * len(VerdictValue) + 1
        )

    @pytest.mark.parametrize(
        ("constraint", "overrides"),
        [
            ("criteria_layer", {"criteria_layer": "teacher"}),
            ("item_kind", {"item_kind": "section"}),
            ("item_id_shape", {"item_id": "c1.p1"}),
            ("item_id_shape", {"item_id": "c0"}),
            (
                "item_id_shape",
                {"item_kind": "point", "item_id": "c1", "weight": None},
            ),
            ("weight_of_criterion", {"weight": None}),
            ("weight_of_criterion", {"weight": "important"}),
            (
                "weight_of_criterion",
                {"item_kind": "point", "item_id": "c1.p1", "weight": "must"},
            ),
            ("verdict", {"verdict": "partly"}),
            ("model_verdict", {"model_verdict": "partly"}),
            (
                "point_judged_by_model",
                {
                    "item_kind": "point",
                    "item_id": "c1.p1",
                    "weight": None,
                    "model_verdict": None,
                },
            ),
            (
                "met_has_quote",
                {
                    "quote": None,
                    "quote_file": None,
                    "quote_line_start": None,
                    "quote_line_end": None,
                },
            ),
            ("place_of_a_quote", {"quote": None, "model_verdict": None}),
            ("lines", {"quote_line_end": None}),
            ("lines", {"quote_line_start": None}),
            ("lines", {"quote_line_start": 0, "quote_line_end": 0}),
            ("lines", {"quote_line_start": 5, "quote_line_end": 4}),
            (
                "lines",
                {"quote_file": None, "quote_line_start": 1, "quote_line_end": 1},
            ),
            ("safeguard_source", {"safeguard_fired": True}),
            ("safeguard_source", {"safeguard_submission_id": uuid.uuid4()}),
            (
                "safeguard_keeps_met",
                {
                    "safeguard_fired": True,
                    "safeguard_submission_id": uuid.uuid4(),
                    "verdict": "not_met",
                },
            ),
            ("unfound_quote_not_met", {"quote_not_found": True}),
        ],
    )
    async def test_a_row_breaking_a_rule_is_refused_by_that_rule(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        constraint: str,
        overrides: dict[str, Any],
    ) -> None:
        name = f"ck_submission_criterion_verdicts_{constraint}"

        with pytest.raises(IntegrityError, match=name):
            await _write(db_session, _met_criterion(submission, **overrides))

    async def test_a_derived_criterion_needs_no_quote(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        """A criterion checked by points has no model verdict and no quote of
        its own — the "met" it gets from its points is still admitted."""
        await _write(
            db_session,
            _met_criterion(
                submission,
                model_verdict=None,
                quote=None,
                quote_file=None,
                quote_line_start=None,
                quote_line_end=None,
            ),
        )

    async def test_the_safeguard_may_keep_an_item_whose_new_quote_was_not_found(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        """The model said "met" with a quote nobody found, and the safeguard
        kept the earlier "met": both flags stand on one row."""
        await _write(
            db_session,
            _met_criterion(
                submission,
                quote_not_found=True,
                safeguard_fired=True,
                safeguard_submission_id=uuid.uuid4(),
            ),
        )

    async def test_one_row_per_item_of_a_submission(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        await _write(db_session, _met_criterion(submission))

        with pytest.raises(
            IntegrityError, match="uq_submission_criterion_verdicts_submission_item"
        ):
            await _write(db_session, _met_criterion(submission))


class TestTheDatabaseHoldsTheExplanationRules:
    async def test_one_explanation_per_submission(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        for _ in range(2):
            db_session.add(
                SubmissionExplanation(
                    tenant_id=submission.tenant_id,
                    submission_id=submission.id,
                    language="ukr",
                    body={"why": "x"},
                )
            )
        with pytest.raises(
            IntegrityError, match="uq_submission_explanations_submission_id"
        ):
            await db_session.flush()

    @pytest.mark.parametrize("language", ["uk", "UKR", "u1k", ""])
    async def test_a_language_that_is_not_a_three_letter_code_is_refused(
        self, db_session: AsyncSession, submission: HomeworkSubmission, language: str
    ) -> None:
        db_session.add(
            SubmissionExplanation(
                tenant_id=submission.tenant_id,
                submission_id=submission.id,
                language=language,
                body={},
            )
        )
        with pytest.raises(IntegrityError, match="ck_submission_explanations_language"):
            await db_session.flush()
