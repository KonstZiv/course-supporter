"""Two attempts of a text task, the second evaluated with the first's memory.

Task 13, K4. Against a live database, with the models as doubles:

* through the body — the ARQ task, the seam, the doors, the attempt ledger,
  the shipped stages and file: a ``task``'s second attempt walks
  ``repeat_without_replies``, and its evaluation's first request shows the
  line difference from the first attempt's work — read back from its own
  file by the doors' code — and the first attempt's verdicts; the first
  attempt's request is v1's byte for byte;
* the stage with replies (``criteria_evaluation_with_replies``) on a second
  attempt: the replies and the memory both, the memory read by production's
  source (:class:`~course_supporter.homework.earlier_review.DoorsEarlierWork`)
  over an S3 double — and with another version of the task, its note;
* the explanation reads the earlier attempt as it did: the same version only.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.tasks import arq_process_homework
from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.homework.criteria_evaluation import evaluate_criteria
from course_supporter.homework.criteria_form import criteria_to_document
from course_supporter.homework.criteria_list_service import CriteriaInForce
from course_supporter.homework.earlier_review import DoorsEarlierWork
from course_supporter.homework.path_config import PathConfig, PathKey, SubmissionState
from course_supporter.homework.path_runner import read_text_work
from course_supporter.homework.path_stages import (
    CRITERIA_EVALUATION,
    CRITERIA_EVALUATION_WITH_REPLIES,
    REVIEW_EXPLANATION,
    StageContext,
    StageOutcome,
    _execution,
)
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.llm.stage_router import StageResult
from course_supporter.models.source import AssignmentType
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Student,
    Tenant,
)
from course_supporter.storage.student_repository import StudentRepository
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
)
from course_supporter.storage.task_criteria_list_repository import (
    TaskCriteriaListRepository,
)
from course_supporter.storage.task_criteria_override_repository import (
    TaskCriteriaOverrideRepository,
)
from tests._helpers.course_node_factory import make_root_course_node
from tests.integration.test_reply_stages_db import (
    _CRITERIA as _REPLY_CRITERIA,
)
from tests.integration.test_reply_stages_db import (
    _RECORDS,
    _met,
    _not_met,
    _Replies,
    _Router,
    _verdicts,
)
from tests.integration.test_text_review_e2e_db import (
    _CRITERIA as _E2E_CRITERIA,
)
from tests.integration.test_text_review_e2e_db import (
    _EXPLANATION,
    _evaluation,
    _ListSource,
    _PortDouble,
    _RouterDouble,
    _shipped,
    _submission,
    _submit,
    _verdict,
    _webhook,
)

pytestmark = pytest.mark.requires_db

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Ten lines; the second attempt changes one and adds one: 2 + 1 of 11 lines,
# well under the share of 0.5 — the difference is shown.
_FIRST_WORK = (
    "def solve(n):\n"
    "    if n < 0:\n"
    '        raise ValueError("n must not be negative")\n'
    "    return n * 2\n"
    "\n"
    "\n"
    "def main():\n"
    "    print(solve(2))\n"
    "    print(solve(3))\n"
    "    print(solve(4))\n"
)
_SECOND_WORK = _FIRST_WORK.replace(
    "def solve(n):\n", 'def solve(n):\n    """Doubles n."""\n'
).replace("    return n * 2\n", "    return n + n\n")


class _S3:
    """Each submission's file under its own key; every download a new file."""

    def __init__(self, tmp_path: Path, files: dict[str, str]) -> None:
        self._dir = tmp_path
        self.files = files
        self.downloaded: list[str] = []
        self._made: list[Path] = []

    def left_behind(self) -> list[Path]:
        """The downloads still on disk: the reader deletes its own."""
        return [path for path in self._made if path.exists()]

    def extract_key(self, url: str) -> str | None:
        return url.removeprefix("s3://bucket/")

    async def download_file(self, key: str, dest: Path | None = None) -> Path:
        self.downloaded.append(key)
        path = self._dir / f"{uuid.uuid4().hex}.download"
        path.write_text(self.files[key], encoding="utf-8")
        self._made.append(path)
        return path


class _Requests(_RouterDouble):
    """The e2e router double, keeping every request: stage, prompt, variables."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def execute_stage(
        self, stage: Any, stage_name: str, /, **kwargs: Any
    ) -> StageResult:
        render = {
            key: value
            for key, value in kwargs.items()
            if key
            not in {
                "response_validator",
                "contents",
                "expects_json",
                "response_schema",
                "stop_on_output_ceiling",
                "money_ceiling_usd",
                "repeat_on_rung",
            }
        }
        self.requests.append((stage_name, stage.prompt_ref, render))
        return await super().execute_stage(stage, stage_name, **kwargs)

    def evaluations(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (ref, render)
            for name, ref, render in self.requests
            if name == CRITERIA_EVALUATION
        ]


def _rendered(ref: str, variables: dict[str, Any]) -> tuple[str, str]:
    prompt = load_prompt(ref, base_path=_REPO_ROOT).render(**variables)
    assert prompt.system is not None
    assert prompt.user is not None
    return prompt.system, prompt.user


def _slot(user: str, name: str) -> str:
    [body] = re.findall(rf"^<{name}>\n(.*?)\n</{name}>$", user, flags=re.S | re.M)
    return str(body)


# ── Through the body ─────────────────────────────────────────────────────────


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, Any]]:
    """A versioned ``task`` with a ready model list, and one student."""
    async with session_factory() as session:
        tenant = Tenant(name=f"memory-e2e-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = CourseNode(
            tenant_id=tenant.id, title="Course", order=0, default_language="ukr"
        )
        session.add(node)
        await session.flush()
        version = "b" * 64
        doc = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="text",
            source_url=f"https://example.com/{uuid.uuid4().hex}",
            task_type="task",
            language="ukr",
            content_hash=version,
        )
        session.add(doc)
        await session.flush()
        lists = TaskCriteriaListRepository(session)
        claimed = await lists.claim(
            authored_document_id=doc.id,
            source_content_hash=version,
            source_task_type="task",
            form_version=2,
        )
        assert claimed is not None
        assert await lists.mark_ready(
            claimed.id,
            claimed_at=claimed.claimed_at,
            criteria=criteria_to_document(_E2E_CRITERIA),
            contradictions=[],
            concepts_in_input=False,
            dropped_concept_count=0,
            input_fingerprint="f" * 64,
        )
        student = Student(
            tenant_id=tenant.id, external_id=f"stu-{uuid.uuid4().hex[:6]}"
        )
        session.add(student)
        await session.commit()
        ids: dict[str, Any] = {
            "tenant_id": tenant.id,
            "node_id": node.id,
            "doc_id": doc.id,
            "student_id": student.id,
            "in_force": CriteriaInForce(
                criteria=_E2E_CRITERIA, layer=CriteriaLayer.MODEL, source_id=claimed.id
            ),
            "submissions": [],
        }

    yield ids

    async with session_factory() as session:
        for submission_id in ids["submissions"]:
            job_ids = list(
                (
                    await session.execute(
                        select(Job.id).where(Job.subject_id == submission_id)
                    )
                ).scalars()
            )
            await session.execute(
                delete(ExternalServiceCall).where(
                    ExternalServiceCall.job_id.in_(job_ids)
                )
            )
            await session.execute(
                delete(HomeworkSubmission).where(HomeworkSubmission.id == submission_id)
            )
            await session.execute(delete(Job).where(Job.subject_id == submission_id))
        await session.commit()


async def _attempt(
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    s3: _S3,
    *,
    n: int,
    work: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A submission whose file is its own in S3: (submission, job)."""
    submission_id, job_id = await _submit(session_factory, world)
    key = f"homework/{n}/answers.py"
    s3.files[key] = work
    async with session_factory() as session:
        await session.execute(
            update(HomeworkSubmission)
            .where(HomeworkSubmission.id == submission_id)
            .values(file_url=f"s3://bucket/{key}")
        )
        await session.commit()
    return submission_id, job_id


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    router: _Requests,
    source: _ListSource,
    s3: _S3,
    *,
    submission_id: uuid.UUID,
    job_id: uuid.UUID,
    config: PathConfig,
) -> None:
    """The ARQ task through the seam, its S3 the double of distinct files."""
    with (
        patch(
            "course_supporter.homework.path_runner.get_path_config",
            return_value=config,
        ),
        patch(
            "course_supporter.homework.path_continuation.get_path_config",
            return_value=config,
        ),
        patch(
            "course_supporter.homework.path_runner.build_funds_port",
            return_value=_PortDouble(),
        ),
        patch(
            "course_supporter.homework.criteria_evaluation.build_criteria_list_service",
            return_value=source,
        ),
        patch("course_supporter.homework.webhook.deliver_webhook", new=_webhook()),
    ):
        await arq_process_homework(
            {
                "session_factory": session_factory,
                "stage_router": router,
                "s3_client": s3,
                "job_try": 1,
            },
            str(job_id),
            str(submission_id),
        )


class TestTheSecondAttemptIsEvaluatedWithTheFirstsMemory:
    async def test_the_difference_and_the_earlier_verdicts_are_in_its_request(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        config = _shipped()
        router = _Requests(session_factory)
        source = _ListSource(world["in_force"])
        s3 = _S3(tmp_path, {})
        # c2 (by its points) and c3 not met on both attempts: one explanation.
        router.content[REVIEW_EXPLANATION] = _EXPLANATION

        # ── Attempt 1: c1 and c2.p1 met, c2.p2 and c3 not ──
        router.content[CRITERIA_EVALUATION] = _evaluation(
            _verdict("c1", "met", quote="def solve(n):"),
            _verdict("c2.p1", "met", quote="return n * 2"),
            _verdict("c2.p2", "not_met", missing="Немає тестів."),
            _verdict("c3", "not_met", missing="Немає докстрінга."),
        )
        first_id, first_job = await _attempt(
            session_factory, world, s3, n=1, work=_FIRST_WORK
        )
        await _run(
            session_factory,
            router,
            source,
            s3,
            submission_id=first_id,
            job_id=first_job,
            config=config,
        )
        first = await _submission(session_factory, first_id)
        assert (first.status, first.attempt_no) == ("delivered", 1)

        # A first attempt: v2's request is v1's, byte for byte, no memory.
        [(ref, variables)] = router.evaluations()
        assert ref == "prompts/criteria_evaluation/v2.md"
        assert "previous_attempt" not in variables
        assert _rendered(ref, variables) == _rendered(
            "prompts/criteria_evaluation/v1.md", variables
        )

        # ── Attempt 2: the work changed ──
        router.content[CRITERIA_EVALUATION] = _evaluation(
            _verdict("c1", "met", quote="def solve(n):"),
            _verdict("c2.p1", "met", quote="return n + n"),
            _verdict("c2.p2", "not_met", missing="Немає тестів."),
            _verdict("c3", "not_met", missing="Докстрінг не пояснює винятку."),
        )
        second_id, second_job = await _attempt(
            session_factory, world, s3, n=2, work=_SECOND_WORK
        )
        await _run(
            session_factory,
            router,
            source,
            s3,
            submission_id=second_id,
            job_id=second_job,
            config=config,
        )
        second = await _submission(session_factory, second_id)
        assert (second.status, second.attempt_no) == ("delivered", 2)
        assert second.task_version == first.task_version
        # The doors read the earlier attempt's own file, after this one's.
        assert s3.downloaded == [
            "homework/1/answers.py",
            "homework/2/answers.py",
            "homework/1/answers.py",
        ]

        ref, variables = router.evaluations()[-1]
        assert ref == "prompts/criteria_evaluation/v2.md"
        system, user = _rendered(ref, variables)
        diff = _slot(user, "previous_attempt_diff")
        assert '+    """Doubles n."""' in diff
        assert "-    return n * 2" in diff
        assert "+    return n + n" in diff
        assert json.loads(_slot(user, "previous_attempt_verdicts")) == [
            {"id": "c1", "quote": "def solve(n):", "verdict": "met"},
            {"id": "c2.p1", "quote": "return n * 2", "verdict": "met"},
            {"id": "c2.p2", "missing": "Немає тестів.", "verdict": "not_met"},
            {"id": "c3", "missing": "Немає докстрінга.", "verdict": "not_met"},
        ]
        assert "### The student's previous attempt" in system
        # The same version of the task, the same list, the work readable:
        # no note.
        assert "changed since the previous attempt" not in user
        assert "could not be read" not in user

        # The review's rows: what the model said, settled as ever.
        rows = await _verdict_rows(session_factory, second_id)
        assert rows == {
            "c1": "met",
            "c2": "not_met",
            "c2.p1": "met",
            "c2.p2": "not_met",
            "c3": "not_met",
        }


async def _verdict_rows(
    session_factory: async_sessionmaker[AsyncSession], submission_id: uuid.UUID
) -> dict[str, str]:
    async with session_factory() as session:
        submission = await session.get(HomeworkSubmission, submission_id)
        assert submission is not None
        rows = await SubmissionVerdictRepository(session).list_for_submission(
            tenant_id=submission.tenant_id, submission_id=submission_id
        )
        return {row.item_id: row.verdict for row in rows}


# ── The stage with replies ───────────────────────────────────────────────────

_REPLY_FIRST = "def fibonacci(n):\n    if n < 2:\n        return n\n    return n\n"
_REPLY_SECOND = (
    "def fibonacci(n):\n"
    "    if n < 2:\n"
    "        return n\n"
    "    return fibonacci(n - 1) + fibonacci(n - 2)\n"
)


class _Criteria:
    """The criteria source double: one list in force, at a fixed address."""

    def __init__(self, in_force: CriteriaInForce) -> None:
        self.in_force = in_force

    async def get_or_compose(self, authored_document_id: uuid.UUID) -> CriteriaInForce:
        return self.in_force


@pytest.fixture()
async def two_attempts(
    db_session: AsyncSession, seed_tenant: Tenant, tmp_path: Path
) -> dict[str, Any]:
    """A first attempt reviewed — rows on the author's list — and a second."""
    root = make_root_course_node(tenant_id=seed_tenant.id, title="Курс", order=0)
    db_session.add(root)
    await db_session.flush()
    doc = AuthoredDocument(
        course_node_id=root.id,
        course_root_id=root.id,
        source_type="text",
        source_url=f"https://example.com/{uuid.uuid4().hex}",
        task_type="task",
        language="ukr",
    )
    db_session.add(doc)
    await db_session.flush()
    student = await StudentRepository(db_session).create(
        tenant_id=seed_tenant.id, external_id=f"ext-{uuid.uuid4().hex[:8]}"
    )
    submissions = HomeworkRepository(db_session)
    made = []
    for n in (1, 2):
        made.append(
            await submissions.create(
                tenant_id=seed_tenant.id,
                student_id=student.id,
                course_node_id=root.id,
                node_id=root.id,
                authored_document_id=doc.id,
                file_url=f"s3://bucket/homework/{n}/solution.py",
                file_type="text/plain",
                original_filename="solution.py",
            )
        )
    first, second = made
    first.status = "delivered"
    first.task_version = "v1"
    second.task_version = "v1"
    edit = await TaskCriteriaOverrideRepository(db_session).replace(
        authored_document_id=doc.id,
        source_content_hash="h" * 64,
        source_task_type="task",
        criteria=criteria_to_document(_REPLY_CRITERIA),
    )
    in_force = CriteriaInForce(
        criteria=_REPLY_CRITERIA, layer=CriteriaLayer.AUTHOR, source_id=edit.id
    )
    await SubmissionVerdictRepository(db_session).replace_for_submission(
        tenant_id=first.tenant_id,
        submission_id=first.id,
        criteria_layer=in_force.layer,
        criteria_source_id=in_force.source_id,
        records=_RECORDS,
    )
    await db_session.flush()
    s3 = _S3(
        tmp_path,
        {
            "homework/1/solution.py": _REPLY_FIRST,
            "homework/2/solution.py": _REPLY_SECOND,
        },
    )
    return {"first": first, "second": second, "in_force": in_force, "s3": s3}


async def _evaluate_with_replies(
    session: AsyncSession, attempts: dict[str, Any], router: _Router
) -> StageOutcome:
    second: HomeworkSubmission = attempts["second"]
    reading = read_text_work(
        filename="solution.py",
        file_bytes=_REPLY_SECOND.encode(),
        languages=["ukr"],
        note_flags=(),
    )
    config = _shipped()
    context = StageContext(
        session=session,
        router=router,  # type: ignore[arg-type]
        submission=second,
        submission_text=reading.text,
        language="ukr",
        path_key=PathKey(AssignmentType.TASK, SubmissionState.REPEAT_WITH_REPLIES),
        stage_name=CRITERIA_EVALUATION_WITH_REPLIES,
        stage=config.stages[CRITERIA_EVALUATION_WITH_REPLIES],
        session_factory=None,  # type: ignore[arg-type]
        door=reading,
        work=reading.files,
        config=config,
        earlier_work=DoorsEarlierWork(attempts["s3"]),
    )
    return await evaluate_criteria(
        context,
        execution=_execution(context),
        criteria_source=_Criteria(attempts["in_force"]),
        reply_source=_Replies(),
    )


def _request(router: _Router) -> tuple[str, str]:
    (request,) = router.requests
    assert request["prompt_ref"] == "prompts/criteria_evaluation/v2_with_replies.md"
    return _rendered(request["prompt_ref"], request["render"])


class TestTheStageWithRepliesShowsTheMemoryToo:
    async def test_the_replies_the_difference_and_the_earlier_verdicts(
        self, db_session: AsyncSession, two_attempts: dict[str, Any]
    ) -> None:
        router = _Router(
            _verdicts(
                _met("c1", "def fibonacci(n):"),
                _met("c2", "return fibonacci(n - 1) + fibonacci(n - 2)"),
            )
        )

        outcome = await _evaluate_with_replies(db_session, two_attempts, router)

        assert outcome == StageOutcome.ok()
        system, user = _request(router)
        assert "<student_replies>" in user
        diff = _slot(user, "previous_attempt_diff")
        assert "-    return n" in diff
        assert "+    return fibonacci(n - 1) + fibonacci(n - 2)" in diff
        assert json.loads(_slot(user, "previous_attempt_verdicts")) == [
            {"id": "c1", "quote": "def fibonacci(n):", "verdict": "met"},
            {"id": "c2", "missing": "Рекурсії немає.", "verdict": "not_met"},
        ]
        assert "`<previous_attempt_diff>`" in system
        assert "- The assignment has changed" not in user
        # The doors read the earlier attempt's own file; its download is gone.
        assert two_attempts["s3"].downloaded == ["homework/1/solution.py"]
        assert two_attempts["s3"].left_behind() == []

    async def test_another_version_of_the_task_is_a_note(
        self, db_session: AsyncSession, two_attempts: dict[str, Any]
    ) -> None:
        two_attempts["second"].task_version = "v2"
        await db_session.flush()
        router = _Router(_verdicts(_met("c1", "def fibonacci(n):"), _not_met("c2")))

        await _evaluate_with_replies(db_session, two_attempts, router)

        _, user = _request(router)
        assert "- The assignment has changed since the previous attempt." in user
        assert _slot(user, "previous_attempt_diff")
        assert _slot(user, "previous_attempt_verdicts")
        # The explanation's earlier attempt is still of the same version alone.
        stored = SubmissionVerdictRepository(db_session)
        assert (
            await stored.earlier_attempt(
                tenant_id=two_attempts["second"].tenant_id,
                submission_id=two_attempts["second"].id,
            )
            is None
        )
        found = await stored.earlier_attempt(
            tenant_id=two_attempts["second"].tenant_id,
            submission_id=two_attempts["second"].id,
            same_version=False,
        )
        assert found is not None
        assert found.submission_id == two_attempts["first"].id
