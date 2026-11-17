"""The traces of revision ``esc_request_traces``, as the database holds them.

Requires ``docker compose up -d`` (PostgreSQL), migrated to head.

What only a live database answers:

* the columns are the ORM's — type, nullability, comment; the foreign key of
  a verdict to its call is ``ON DELETE SET NULL`` (the comments of the verdict
  table's columns, the two new among them, are compared column by column in
  ``test_submission_verdict_schema_db.py``);
* each new ``CHECK`` refuses the one row that breaks it and admits every
  member of the vocabulary it is built from;
* the router writes the traces through the real ``_persist``, and the id its
  result names is a row that is there;
* the three comparisons the traces open — the cost of repeats by reason and
  model, the share of repeats that changed a verdict, the verdicts and the
  stage's cost by model — run on this database and count what was written.
  The queries are the ones handed over with the change; a column renamed
  under them turns this red.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Table, delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.call_outcome import RequestKind
from course_supporter.criteria_kinds import (
    CriteriaLayer,
    RepeatReason,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.llm.ladder_config import LadderConfig, LadderEntry, StageConfig
from course_supporter.llm.prompt_loader_md import StagePrompt
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.schemas import LLMResponse
from course_supporter.llm.stage_router import StageRouter
from course_supporter.service_logging import job_scope
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    SubmissionCriterionVerdict,
    Tenant,
)
from course_supporter.storage.student_repository import StudentRepository
from tests._helpers.registry import empty_registry

pytestmark = pytest.mark.requires_db

_ESC = cast(Table, ExternalServiceCall.__table__)
_VERDICTS = cast(Table, SubmissionCriterionVerdict.__table__)

# (column, data_type) as information_schema names them; every one nullable.
_ESC_COLUMNS = {
    "request_kind": "character varying",
    "seed": "integer",
    "temperature": "double precision",
    "rung_index": "smallint",
}
_VERDICT_COLUMNS = {
    "first_model_verdict": "character varying",
    "answered_by_call_id": "uuid",
}

# ── The three comparisons (handed over with the change) ────────────────

# 1. The cost of repeats by reason and model. A structural or an
#    infrastructure retry is its own reason; an item repeat asks about every
#    retried item of its submission at once, so its price is shared among
#    them by the reason each was asked again.
COST_OF_REPEATS_SQL = """
WITH repeats AS (
    SELECT e.id, e.job_id, e.action, e.provider, e.model_id, e.request_kind,
           e.cost_usd
    FROM external_service_calls e
    WHERE e.request_kind IN ('item_repeat', 'structural_retry', 'infra_retry')
),
reasons AS (
    SELECT j.id AS job_id, coalesce(v.repeat_reason, 'unknown') AS repeat_reason,
           count(*)::numeric / sum(count(*)) OVER (PARTITION BY j.id) AS share
    FROM jobs j
    JOIN submission_criterion_verdicts v
      ON v.submission_id = j.subject_id AND v.retried
    WHERE j.subject_type = 'homework_submission'
    GROUP BY j.id, coalesce(v.repeat_reason, 'unknown')
)
SELECT r.action, r.request_kind,
       CASE WHEN r.request_kind = 'item_repeat'
            THEN coalesce(x.repeat_reason, 'unknown')
            ELSE r.request_kind END AS reason,
       r.provider, r.model_id,
       count(DISTINCT r.id) AS calls,
       sum(r.cost_usd * coalesce(x.share, 1)) AS cost_usd,
       count(DISTINCT r.id) FILTER (WHERE r.cost_usd IS NULL) AS unpriced_calls
FROM repeats r
LEFT JOIN reasons x ON r.request_kind = 'item_repeat' AND x.job_id = r.job_id
GROUP BY 1, 2, 3, 4, 5
ORDER BY cost_usd DESC NULLS LAST
"""

# 2. Did the repeat change the verdict — the share, by reason and model. The
#    model's word an item settled on against its first word, and the verdict
#    that stands against the first word.
REPEAT_CHANGED_VERDICT_SQL = """
SELECT coalesce(v.repeat_reason, 'unknown') AS reason,
       e.provider, e.model_id,
       count(*) AS retried_items,
       count(*) FILTER (WHERE v.model_verdict <> v.first_model_verdict)
           AS model_word_changed,
       round(avg((v.model_verdict <> v.first_model_verdict)::int), 4)
           AS share_model_word_changed,
       round(avg((v.verdict <> v.first_model_verdict)::int), 4)
           AS share_standing_verdict_differs
FROM submission_criterion_verdicts v
LEFT JOIN external_service_calls e ON e.id = v.answered_by_call_id
WHERE v.retried AND v.first_model_verdict IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
"""

# 3. Verdicts and the stage's cost by model (and prompt version): a
#    submission counts for the model whose first answer its verdicts stand
#    on; its cost is every attempt of the stage for it, whatever rung paid.
VERDICTS_AND_COST_BY_MODEL_SQL = """
WITH answering AS (
    SELECT DISTINCT ON (v.submission_id)
           v.submission_id, e.action, e.provider, e.model_id, e.prompt_hash
    FROM submission_criterion_verdicts v
    JOIN external_service_calls e ON e.id = v.answered_by_call_id
    ORDER BY v.submission_id, (e.request_kind = 'first') DESC, e.created_at
),
items AS (
    SELECT submission_id, count(*) AS items,
           count(*) FILTER (WHERE verdict = 'met') AS met,
           count(*) FILTER (WHERE retried) AS retried
    FROM submission_criterion_verdicts
    GROUP BY submission_id
),
spent AS (
    SELECT j.subject_id AS submission_id, e.action,
           sum(e.cost_usd) AS cost_usd, count(*) AS attempts
    FROM external_service_calls e
    JOIN jobs j ON j.id = e.job_id AND j.subject_type = 'homework_submission'
    WHERE e.request_kind IS NOT NULL
    GROUP BY j.subject_id, e.action
)
SELECT a.action, a.provider, a.model_id, a.prompt_hash,
       count(*) AS submissions,
       sum(i.items) AS items,
       round(sum(i.met)::numeric / sum(i.items), 4) AS share_met,
       round(sum(i.retried)::numeric / sum(i.items), 4) AS share_retried,
       sum(s.attempts) AS attempts,
       sum(s.cost_usd) AS stage_cost_usd,
       round(sum(s.cost_usd) / count(*), 6) AS cost_per_submission_usd
FROM answering a
JOIN items i USING (submission_id)
LEFT JOIN spent s ON s.submission_id = a.submission_id AND s.action = a.action
GROUP BY a.action, a.provider, a.model_id, a.prompt_hash
ORDER BY a.action, cost_per_submission_usd
"""


async def _columns(session: AsyncSession, table: str) -> dict[str, dict[str, Any]]:
    result = await session.execute(
        text(
            "SELECT c.column_name, c.data_type, c.is_nullable, "
            "col_description(CAST(:relation AS regclass), c.ordinal_position) "
            "FROM information_schema.columns c "
            "WHERE c.table_name = :table"
        ),
        {"relation": table, "table": table},
    )
    return {
        str(name): {"type": kind, "nullable": nullable == "YES", "comment": comment}
        for name, kind, nullable, comment in result.all()
    }


class TestTheColumnsAreTheOrms:
    @pytest.mark.parametrize(
        ("table", "columns"),
        [(_ESC, _ESC_COLUMNS), (_VERDICTS, _VERDICT_COLUMNS)],
        ids=["external_service_calls", "submission_criterion_verdicts"],
    )
    async def test_type_nullability_and_comment(
        self, db_session: AsyncSession, table: Table, columns: dict[str, str]
    ) -> None:
        live = await _columns(db_session, table.name)

        for name, kind in columns.items():
            assert live[name]["type"] == kind, name
            assert live[name]["nullable"] is True, name
            assert table.c[name].nullable is True, name
            assert live[name]["comment"] == table.c[name].comment, name

    async def test_the_checks_are_the_orms(self, db_session: AsyncSession) -> None:
        names = {
            "ck_esc_request_kind",
            "ck_esc_rung_index",
            "ck_submission_criterion_verdicts_first_model_verdict",
        }
        live = await db_session.execute(
            text("SELECT conname FROM pg_constraint WHERE conname = ANY(:names)"),
            {"names": list(names)},
        )
        orm = {
            str(constraint.name)
            for table in (_ESC, _VERDICTS)
            for constraint in table.constraints
        }

        assert set(live.scalars()) == names
        assert names <= orm

    async def test_a_verdicts_call_key_sets_null(
        self, db_session: AsyncSession
    ) -> None:
        live = await db_session.execute(
            text(
                "SELECT confdeltype, confrelid::regclass::text "
                "FROM pg_constraint WHERE conname = :name"
            ),
            {"name": "fk_submission_criterion_verdicts_answered_by_call_id"},
        )
        (key,) = [fk for fk in _VERDICTS.c.answered_by_call_id.foreign_keys]

        assert tuple(live.one()) == ("n", "external_service_calls")
        assert key.ondelete == "SET NULL"
        assert key.name == "fk_submission_criterion_verdicts_answered_by_call_id"


# ── Rows ────────────────────────────────────────────────────────────────


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


async def _job(session: AsyncSession, submission: HomeworkSubmission) -> uuid.UUID:
    job = Job(
        tenant_id=submission.tenant_id,
        job_type="homework_processing",
        subject_type="homework_submission",
        subject_id=submission.id,
    )
    session.add(job)
    await session.flush()
    return job.id


async def _call(session: AsyncSession, job_id: uuid.UUID, **fields: Any) -> uuid.UUID:
    row: dict[str, Any] = {
        "job_id": job_id,
        "action": "criteria_evaluation",
        "provider": "dashscope",
        "model_id": "qwen3.7-max",
        "prompt_hash": "a" * 64,
        "success": True,
        "request_kind": RequestKind.FIRST.value,
        "seed": 20261113,
        "temperature": 0.0,
        "rung_index": 1,
    }
    row.update(fields)
    if "cost_usd" in row:
        row.setdefault("cost_kind", "estimated")
    call = ExternalServiceCall(**row)
    session.add(call)
    await session.flush()
    return call.id


def _verdict(
    submission: HomeworkSubmission, item_id: str, **fields: Any
) -> SubmissionCriterionVerdict:
    row: dict[str, Any] = {
        "tenant_id": submission.tenant_id,
        "submission_id": submission.id,
        "criteria_layer": CriteriaLayer.MODEL.value,
        "criteria_source_id": uuid.UUID(int=1),
        "item_id": item_id,
        "item_kind": VerdictItemKind.CRITERION.value,
        "weight": WeightCategory.MUST.value,
        "verdict": VerdictValue.NOT_MET.value,
        "model_verdict": VerdictValue.NOT_MET.value,
        "missing": "Not done.",
    }
    row.update(fields)
    return SubmissionCriterionVerdict(**row)


class TestTheDatabaseHoldsTheNewRules:
    @pytest.mark.parametrize("kind", list(RequestKind))
    async def test_every_request_kind_is_admitted(
        self, db_session: AsyncSession, submission: HomeworkSubmission, kind: str
    ) -> None:
        await _call(db_session, await _job(db_session, submission), request_kind=kind)

    async def test_a_row_of_no_call_carries_none(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        await _call(
            db_session,
            await _job(db_session, submission),
            provider=None,
            model_id=None,
            success=None,
            request_kind=None,
            seed=None,
            temperature=None,
            rung_index=None,
        )

    @pytest.mark.parametrize(
        ("constraint", "overrides"),
        [
            ("ck_esc_request_kind", {"request_kind": "second"}),
            ("ck_esc_rung_index", {"rung_index": 0}),
            ("ck_esc_rung_index", {"rung_index": -1}),
        ],
    )
    async def test_a_register_row_breaking_a_rule_is_refused_by_that_rule(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        constraint: str,
        overrides: dict[str, Any],
    ) -> None:
        job_id = await _job(db_session, submission)
        with pytest.raises(IntegrityError, match=constraint):
            await _call(db_session, job_id, **overrides)

    @pytest.mark.parametrize("value", list(VerdictValue))
    async def test_every_first_verdict_is_admitted_on_a_retried_row(
        self, db_session: AsyncSession, submission: HomeworkSubmission, value: str
    ) -> None:
        db_session.add(
            _verdict(
                submission,
                "c1",
                retried=True,
                repeat_reason=RepeatReason.NO_SENTENCE.value,
                first_model_verdict=value,
            )
        )
        await db_session.flush()

    @pytest.mark.parametrize(
        "overrides",
        [
            {"retried": True, "first_model_verdict": "partly"},
            # A first answer only on a retried row.
            {"first_model_verdict": VerdictValue.MET.value},
        ],
        ids=["value", "not-retried"],
    )
    async def test_a_first_verdict_breaking_its_rule_is_refused_by_it(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        overrides: dict[str, Any],
    ) -> None:
        db_session.add(_verdict(submission, "c1", **overrides))
        with pytest.raises(
            IntegrityError,
            match="ck_submission_criterion_verdicts_first_model_verdict",
        ):
            await db_session.flush()

    async def test_a_verdict_cannot_name_a_call_that_is_not_there(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        db_session.add(_verdict(submission, "c1", answered_by_call_id=uuid.uuid4()))
        with pytest.raises(
            IntegrityError,
            match="fk_submission_criterion_verdicts_answered_by_call_id",
        ):
            await db_session.flush()

    async def test_a_deleted_call_leaves_the_verdict_without_it(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        call_id = await _call(db_session, await _job(db_session, submission))
        verdict = _verdict(submission, "c1", answered_by_call_id=call_id)
        db_session.add(verdict)
        await db_session.flush()

        await db_session.execute(
            delete(ExternalServiceCall).where(ExternalServiceCall.id == call_id)
        )
        await db_session.refresh(verdict)

        assert verdict.answered_by_call_id is None

    async def test_a_deleted_submission_leaves_the_call(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        call_id = await _call(db_session, await _job(db_session, submission))
        db_session.add(_verdict(submission, "c1", answered_by_call_id=call_id))
        await db_session.flush()

        await db_session.execute(
            delete(SubmissionCriterionVerdict).where(
                SubmissionCriterionVerdict.submission_id == submission.id
            )
        )

        assert await db_session.get(ExternalServiceCall, call_id) is not None


# ── The router through the real _persist ───────────────────────────────


@pytest.fixture()
async def committed_job(
    session_factory: async_sessionmaker[AsyncSession],
    committed_seeds: dict[str, uuid.UUID],
) -> AsyncGenerator[uuid.UUID]:
    async with session_factory() as session:
        job = Job(
            tenant_id=committed_seeds["tenant_id"],
            course_node_id=committed_seeds["course_node_id"],
            job_type="document_processing",
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    yield job_id

    async with session_factory() as session:
        await session.execute(
            delete(ExternalServiceCall).where(ExternalServiceCall.job_id == job_id)
        )
        await session.execute(delete(Job).where(Job.id == job_id))
        await session.commit()


class TestTheRouterWritesTheTraces:
    async def test_attempt_and_trace_rows_carry_the_traces(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_job: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "course_supporter.llm.stage_router.load_prompt",
            lambda prompt_ref, *, base_path=None: StagePrompt(system="s", user="u"),
        )
        provider = AsyncMock(spec=LLMProvider)
        provider.enabled = True
        provider.check_request = lambda _request: None
        provider.seed_supported = lambda _request: True
        provider.complete = AsyncMock(
            return_value=LLMResponse(content="ok", provider="b", model_id="m2")
        )
        router = StageRouter(
            LadderConfig(),
            {"b": provider},
            registry=empty_registry(),
            session_factory=session_factory,
        )
        stage = StageConfig(
            prompt_ref="prompts/example/v1.md",
            ladder=[
                LadderEntry(provider="missing", model="m1"),
                LadderEntry(provider="b", model="m2"),
            ],
            deterministic=True,
        )

        with job_scope(committed_job):
            result = await router.execute_stage(stage, "demo")

        async with session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(ExternalServiceCall)
                        .where(ExternalServiceCall.job_id == committed_job)
                        .order_by(ExternalServiceCall.id)
                    )
                ).scalars()
            )
        written = [
            (r.outcome, r.request_kind, r.rung_index, r.seed, r.temperature)
            for r in rows
        ]

        assert written == [
            ("skipped", None, 1, None, None),
            ("success", "first", 2, 20261113, 0.0),
        ]
        assert result.call_id == rows[1].id

    async def test_the_answering_row_is_the_one_named(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_job: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "course_supporter.llm.stage_router.load_prompt",
            lambda prompt_ref, *, base_path=None: StagePrompt(system="s", user="u"),
        )
        provider = AsyncMock(spec=LLMProvider)
        provider.enabled = True
        provider.check_request = lambda _request: None
        provider.complete = AsyncMock(
            return_value=LLMResponse(content="ok", provider="b", model_id="m2")
        )
        router = StageRouter(
            LadderConfig(),
            {"b": provider},
            registry=empty_registry(),
            session_factory=session_factory,
        )

        with job_scope(committed_job):
            result = await router.execute_stage(
                StageConfig(
                    prompt_ref="prompts/example/v1.md",
                    ladder=[LadderEntry(provider="b", model="m2")],
                ),
                "demo",
                repeat_on_rung=2,
            )

        assert result.call_id is not None
        async with session_factory() as session:
            row = await session.get(ExternalServiceCall, result.call_id)
        assert row is not None
        assert (row.job_id, row.outcome, row.request_kind, row.rung_index) == (
            committed_job,
            "success",
            RequestKind.ITEM_REPEAT.value,
            2,
        )
        # Not deterministic: no seed, and the request's own temperature.
        assert (row.seed, row.temperature) == (None, 0.0)


# ── The three comparisons ──────────────────────────────────────────────


@pytest.fixture()
async def evaluated(
    db_session: AsyncSession, submission: HomeworkSubmission
) -> HomeworkSubmission:
    """One submission's evaluation, as the stage leaves it in the two tables.

    Calls: the first request (0.10), a structural retry of it (0.02), an item
    repeat (0.04) after an infrastructure retry of it (0.00 — a 503 the
    provider does not bill). Items: c1 not retried; c2 retried for a quote not
    found, its "met" taken back by the repeat; c3 retried for a missing
    sentence, still "not met"; c4 retried for a quote too short but in the
    work, settled on its first "met".
    """
    job_id = await _job(db_session, submission)
    first = await _call(db_session, job_id, cost_usd=Decimal("0.10"))
    await _call(
        db_session,
        job_id,
        request_kind=RequestKind.STRUCTURAL_RETRY.value,
        cost_usd=Decimal("0.02"),
    )
    await _call(
        db_session,
        job_id,
        request_kind=RequestKind.ITEM_REPEAT.value,
        success=False,
        cost_usd=Decimal("0"),
        cost_kind="exact",
        catalog_version="test",
        tariff_window="flat",
    )
    repeat = await _call(
        db_session,
        job_id,
        request_kind=RequestKind.INFRA_RETRY.value,
        cost_usd=Decimal("0.04"),
    )
    met = VerdictValue.MET.value
    not_met = VerdictValue.NOT_MET.value
    db_session.add_all(
        [
            _verdict(submission, "c1", answered_by_call_id=first),
            _verdict(
                submission,
                "c2",
                retried=True,
                repeat_reason=RepeatReason.QUOTE_NOT_FOUND.value,
                first_model_verdict=met,
                answered_by_call_id=repeat,
            ),
            _verdict(
                submission,
                "c3",
                retried=True,
                repeat_reason=RepeatReason.NO_SENTENCE.value,
                first_model_verdict=not_met,
                answered_by_call_id=repeat,
            ),
            _verdict(
                submission,
                "c4",
                retried=True,
                repeat_reason=RepeatReason.QUOTE_TOO_SHORT.value,
                first_model_verdict=met,
                model_verdict=met,
                missing=None,
                quote_not_found=True,
                quote="if n < 2",
                answered_by_call_id=first,
            ),
        ]
    )
    await db_session.flush()
    return submission


def _by(rows: Any, *keys: str) -> dict[tuple[Any, ...], Any]:
    return {tuple(getattr(row, key) for key in keys): row for row in rows}


class TestTheThreeComparisons:
    async def test_the_cost_of_repeats_by_reason_and_model(
        self, db_session: AsyncSession, evaluated: HomeworkSubmission
    ) -> None:
        rows = _by(
            (await db_session.execute(text(COST_OF_REPEATS_SQL))).all(),
            "request_kind",
            "reason",
        )

        assert rows[("structural_retry", "structural_retry")].cost_usd == Decimal(
            "0.02"
        )
        # The item repeat's 503 cost nothing; its retry is an infra retry.
        assert rows[("infra_retry", "infra_retry")].cost_usd == Decimal("0.04")
        item = {
            reason: row.cost_usd
            for (kind, reason), row in rows.items()
            if kind == "item_repeat"
        }
        assert set(item) == {"quote_not_found", "no_sentence", "quote_too_short"}
        assert sum(item.values()) == 0
        assert all(
            row.provider == "dashscope" and row.model_id == "qwen3.7-max"
            for row in rows.values()
        )

    async def test_the_share_of_repeats_that_changed_the_verdict(
        self, db_session: AsyncSession, evaluated: HomeworkSubmission
    ) -> None:
        rows = _by(
            (await db_session.execute(text(REPEAT_CHANGED_VERDICT_SQL))).all(),
            "reason",
        )

        assert rows[("quote_not_found",)].share_model_word_changed == 1
        assert rows[("no_sentence",)].share_model_word_changed == 0
        # Settled on its first "met"; the verdict that stands is "not met".
        assert rows[("quote_too_short",)].share_model_word_changed == 0
        assert rows[("quote_too_short",)].share_standing_verdict_differs == 1
        assert sum(row.retried_items for row in rows.values()) == 3

    async def test_verdicts_and_the_stages_cost_by_model(
        self, db_session: AsyncSession, evaluated: HomeworkSubmission
    ) -> None:
        (row,) = (await db_session.execute(text(VERDICTS_AND_COST_BY_MODEL_SQL))).all()

        assert (row.action, row.provider, row.model_id) == (
            "criteria_evaluation",
            "dashscope",
            "qwen3.7-max",
        )
        assert (row.submissions, row.items, row.attempts) == (1, 4, 4)
        assert row.share_met == 0
        assert row.share_retried == Decimal("0.75")
        assert row.stage_cost_usd == Decimal("0.16")
