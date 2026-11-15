"""A project without a change of the student's, on the new path (chain K6).

Through the whole ARQ task -- the real worker, the real database, the real
S3-compatible storage, the real project door against a real base -- with the
project switched onto the new path in the test's own configuration. The stage
executors and the funds port are spies: what is under test is what happens
before them.

* lock v -- a project whose door reads no new or changed text file of the
  student's (nothing changed, files only deleted, a binary file alone changed)
  is refused before anything is paid for: ``rejected`` with the category
  ``no_changes``, no stage executor called, no reserve asked for, no attempt
  recorded -- the same record as the door's oversize refusal -- and the
  portal's answer carries ``rejection.code = "no_changes"``;
* lock g -- a project with a changed file walks its stages as before.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.routes.portal_submissions import _to_detail
from course_supporter.api.tasks import arq_process_homework
from course_supporter.config import get_settings
from course_supporter.funds_port import (
    FundsAnswer,
    SubmissionContext,
    SubmissionOutcome,
)
from course_supporter.homework.path_config import (
    PathConfig,
    ServedBy,
    SubmissionState,
    load_path_config,
)
from course_supporter.homework.path_stages import StageContext, StageOutcome
from course_supporter.homework.project_submission import (
    NO_CHANGES,
    _submission_snapshot_key,
)
from course_supporter.normalizer import manifest_to_jsonb, normalize_archive
from course_supporter.security.exceptions import ErrorCategory
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    ProjectBase,
    Student,
    Tenant,
)
from course_supporter.storage.s3 import S3Client
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_SHIPPED = load_path_config(get_settings().submission_paths_config_path)
_STAGES = ["safety", "attempt_classifier"]

_APP = b"def main():\n    print('hello')\n"
_UTILS = b"def read(path):\n    return open(path).read()\n"
_LOGO = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
_BASE: dict[str, bytes] = {"app.py": _APP, "utils.py": _UTILS, "logo.png": _LOGO}


def _config() -> PathConfig:
    """The shipped stages, with ``project`` served by the new path."""
    types: dict[str, Any] = {
        name: {"served_by": "todays_mentor", "paths": {}}
        for name in ("test", "short_task", "task", "project")
    }
    types["project"] = {
        "served_by": ServedBy.NEW_PATH.value,
        "paths": {state.value: _STAGES for state in SubmissionState},
    }
    return PathConfig.model_validate(
        {
            "stages": {name: _SHIPPED.stages[name].model_dump() for name in _STAGES},
            "task_types": types,
        }
    )


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


class _SpyPort:
    """The funds port, counting what it is asked; it always allows."""

    def __init__(self) -> None:
        self.reserved: list[float] = []

    async def check_and_reserve(
        self, context: SubmissionContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        self.reserved.append(ceiling_estimate_usd)
        return FundsAnswer.allowed()

    async def account_stage_cost(
        self, context: SubmissionContext, stage_cost_usd: float
    ) -> None:
        return None

    async def release_remainder(
        self, context: SubmissionContext, outcome: SubmissionOutcome
    ) -> None:
        return None


class _Run:
    """What one run asked of the stages and of the funds port."""

    def __init__(self) -> None:
        self.port = _SpyPort()
        self.stages: list[str] = []
        self.files: list[tuple[str, ...]] = []

    def executor(self, name: str) -> Callable[[StageContext], Awaitable[StageOutcome]]:
        async def _run(context: StageContext) -> StageOutcome:
            self.stages.append(name)
            self.files.append(tuple(file.name for file in context.work))
            return StageOutcome.ok()

        return _run


@pytest.fixture()
async def s3_client() -> AsyncGenerator[S3Client]:
    s = get_settings()
    client = S3Client(
        endpoint_url=s.s3_endpoint,
        access_key=s.s3_access_key,
        secret_key=s.s3_secret_key.get_secret_value(),
        bucket=s.s3_bucket,
    )
    await client.open()
    try:
        await client.ensure_bucket()
        yield client
    finally:
        await client.close()


@pytest.fixture()
async def submit(
    session_factory: async_sessionmaker[AsyncSession], s3_client: S3Client
) -> AsyncGenerator[Callable[[dict[str, bytes]], Awaitable[dict[str, uuid.UUID]]]]:
    """Seed a project task with a ready base, and one submission of ``files``."""
    made: list[tuple[dict[str, uuid.UUID], list[str]]] = []

    async def _make(files: dict[str, bytes]) -> dict[str, uuid.UUID]:
        s = get_settings()
        prefix = f"homework/k6/{uuid.uuid4().hex}"
        raw_key = f"{prefix}/proj.zip"
        base_key = f"base/k6/{uuid.uuid4().hex}/snapshot.zip"
        base = normalize_archive(_zip(_BASE), archive_kind="zip")
        await s3_client.upload_file(base_key, base.canonical_zip, "application/zip")
        await s3_client.upload_file(raw_key, _zip(files), "application/zip")
        async with session_factory() as session:
            tenant = Tenant(name=f"k6-{uuid.uuid4().hex[:8]}")
            session.add(tenant)
            await session.flush()
            node = make_root_course_node(tenant_id=tenant.id, title="K6", order=0)
            session.add(node)
            await session.flush()
            doc = AuthoredDocument(
                course_node_id=node.id,
                course_root_id=node.id,
                source_type="text",
                source_url="s3://x",
                task_type="project",
                language="ukr",
            )
            session.add(doc)
            await session.flush()
            project_base = ProjectBase(
                authored_document_id=doc.id,
                version=1,
                archive_key="k/v1/original.zip",
                snapshot_key=base_key,
                snapshot_hash=base.snapshot_hash,
                manifest=manifest_to_jsonb(base.manifest),
                state="ready",
            )
            session.add(project_base)
            await session.flush()
            student = Student(
                tenant_id=tenant.id, external_id=f"s-{uuid.uuid4().hex[:6]}"
            )
            session.add(student)
            await session.flush()
            submission = HomeworkSubmission(
                tenant_id=tenant.id,
                student_id=student.id,
                course_node_id=node.id,
                node_id=node.id,
                authored_document_id=doc.id,
                file_url=f"{s.s3_endpoint}/{s.s3_bucket}/{raw_key}",
                file_type="application/zip",
                original_filename="proj.zip",
                status="received",
                response_language="ukr",
                base_id=project_base.id,
            )
            session.add(submission)
            await session.flush()
            job = Job(
                tenant_id=tenant.id,
                course_node_id=node.id,
                job_type="homework_processing",
                subject_type="homework_submission",
                subject_id=submission.id,
                input_params={"submission_id": str(submission.id)},
                status="queued",
            )
            session.add(job)
            await session.flush()
            await session.commit()
            ids = {
                "tenant_id": tenant.id,
                "node_id": node.id,
                "doc_id": doc.id,
                "submission_id": submission.id,
                "job_id": job.id,
            }
        made.append((ids, [raw_key, _submission_snapshot_key(raw_key), base_key]))
        return ids

    yield _make

    for ids, keys in made:
        for key in keys:
            try:
                await s3_client.delete_object(key)
            except Exception:  # noqa: S112 - best-effort cleanup
                continue
        async with session_factory() as session:
            await session.execute(
                delete(ExternalServiceCall).where(
                    ExternalServiceCall.job_id == ids["job_id"]
                )
            )
            for model in (HomeworkSubmission, Job, Student):
                await session.execute(
                    delete(model).where(model.tenant_id == ids["tenant_id"])
                )
            await session.execute(
                delete(ProjectBase).where(
                    ProjectBase.authored_document_id == ids["doc_id"]
                )
            )
            await session.execute(
                delete(AuthoredDocument).where(AuthoredDocument.id == ids["doc_id"])
            )
            await session.execute(
                delete(CourseNode).where(CourseNode.id == ids["node_id"])
            )
            await session.execute(delete(Tenant).where(Tenant.id == ids["tenant_id"]))
            await session.commit()


async def _run(
    ids: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    *,
    budget_chars: int | None = None,
) -> tuple[_Run, HomeworkSubmission]:
    run = _Run()
    ctx = {
        "session_factory": session_factory,
        "stage_router": MagicMock(),
        "s3_client": s3_client,
        "job_try": 1,
    }
    with ExitStack() as stack:
        path_runner = "course_supporter.homework.path_runner"
        stack.enter_context(
            patch(f"{path_runner}.get_path_config", return_value=_config())
        )
        stack.enter_context(
            patch(f"{path_runner}.get_stage_executor", side_effect=run.executor)
        )
        stack.enter_context(
            patch(f"{path_runner}.AlwaysEnoughFundsPort", return_value=run.port)
        )
        # The doors and the stages are under test, not the review: the spies
        # write no verdicts for the builder to read.
        stack.enter_context(
            patch(f"{path_runner}.get_result_builder", return_value=None)
        )
        if budget_chars is not None:
            stack.enter_context(
                patch(
                    "course_supporter.homework.project_submission."
                    "project_context_budget_chars",
                    return_value=budget_chars,
                )
            )
        await arq_process_homework(ctx, str(ids["job_id"]), str(ids["submission_id"]))
    async with session_factory() as session:
        submission = await session.get(HomeworkSubmission, ids["submission_id"])
    assert submission is not None
    return run, submission


_UNCHANGED: dict[str, dict[str, bytes]] = {
    "nothing_changed": dict(_BASE),
    "files_only_deleted": {"app.py": _APP},
    "a_binary_alone_changed": {**_BASE, "logo.png": _LOGO + b"\x00\x01"},
}


class TestAProjectWithoutAChange:
    """Lock v."""

    @pytest.mark.parametrize("files", _UNCHANGED.values(), ids=_UNCHANGED.keys())
    async def test_is_refused_before_anything_is_paid_for(
        self,
        submit: Callable[[dict[str, bytes]], Awaitable[dict[str, uuid.UUID]]],
        session_factory: async_sessionmaker[AsyncSession],
        s3_client: S3Client,
        files: dict[str, bytes],
    ) -> None:
        ids = await submit(files)

        run, submission = await _run(ids, session_factory, s3_client)

        assert submission.status == "rejected"
        assert submission.safety_result is not None
        assert submission.safety_result["source"] == "normalizer"
        assert submission.safety_result["category"] == NO_CHANGES == "no_changes"
        # Nothing paid for: no stage, no reserve -- and no attempt either.
        assert run.stages == []
        assert run.port.reserved == []
        assert submission.attempt_no is None
        assert submission.attempt_receipt is None
        # What the portal answers about it.
        detail = _to_detail(submission)
        assert detail.rejection is not None
        assert detail.rejection.code == "no_changes"
        assert detail.presentation.reason_code == "no_changes"

    async def test_is_recorded_as_the_oversize_refusal_is(
        self,
        submit: Callable[[dict[str, bytes]], Awaitable[dict[str, uuid.UUID]]],
        session_factory: async_sessionmaker[AsyncSession],
        s3_client: S3Client,
    ) -> None:
        """The same status, the same source, the same absence of an attempt
        and of a paid step; the category is each refusal's own."""
        unchanged = await submit(dict(_BASE))
        oversize = await submit({**_BASE, "app.py": _APP + b"# mine\n"})

        runs = {
            NO_CHANGES: await _run(unchanged, session_factory, s3_client),
            ErrorCategory.OVER_BUDGET.value: await _run(
                oversize, session_factory, s3_client, budget_chars=10
            ),
        }

        for category, (run, submission) in runs.items():
            assert submission.status == "rejected", category
            assert submission.safety_result is not None
            assert submission.safety_result["source"] == "normalizer"
            assert submission.safety_result["category"] == category
            assert submission.error_message is not None
            assert submission.error_message.startswith(f"{category}: ")
            assert (run.stages, run.port.reserved) == ([], [])
            assert (submission.attempt_no, submission.attempt_receipt) == (None, None)
            detail = _to_detail(submission)
            assert detail.rejection is not None
            assert detail.rejection.code == category


class TestAProjectWithAChange:
    """Lock g: its stages go as before."""

    async def test_walks_its_stages(
        self,
        submit: Callable[[dict[str, bytes]], Awaitable[dict[str, uuid.UUID]]],
        session_factory: async_sessionmaker[AsyncSession],
        s3_client: S3Client,
    ) -> None:
        ids = await submit({**_BASE, "app.py": _APP + b"# mine\n"})

        run, submission = await _run(ids, session_factory, s3_client)

        assert submission.status in {"completed", "delivered"}, submission.error_message
        assert run.stages == _STAGES
        assert run.files == [("app.py",), ("app.py",)]
        assert len(run.port.reserved) == 1
        assert submission.attempt_no == 1
