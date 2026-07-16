"""Unit locks for the L2 execution seam (``jobs.execution_seam``).

Fast, DB-free: a fake ``JobRepository`` backed by an in-memory store mirrors the
REAL ``JOB_TRANSITIONS`` machine (so transition legality is faithful), and
``_subject_is_alive`` is stubbed to drive the liveness branch. The seam's DB-tier
behaviour (real update_status ValueError, real subjects) is covered by the
repository/integration suites; here we lock the seam's ORCHESTRATION.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from arq import Retry

from course_supporter.jobs import execution_seam as seam
from course_supporter.jobs.execution_seam import (
    SeamTerminal,
    through_seam,
)
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE
from course_supporter.security.exceptions import ErrorCategory
from course_supporter.storage.job_repository import JOB_TRANSITIONS

# ── fakes ────────────────────────────────────────────────────────────────


class _FakeJob:
    def __init__(
        self,
        status: str,
        subject_type: str | None = None,
        subject_id: uuid.UUID | None = None,
    ) -> None:
        self.status = status
        self.subject_type = subject_type
        self.subject_id = subject_id
        self.error_message: str | None = None
        self.error_category: str | None = None
        self.result_data: dict[str, Any] | None = None
        self.started_at: Any = None
        self.completed_at: Any = None


class _FakeRepo:
    """Mirrors the real update_status transition machine over an in-memory store."""

    def __init__(self, session: _FakeSession) -> None:
        self._store = session.store
        self._calls = session.calls

    async def get_by_id(self, jid: uuid.UUID) -> _FakeJob | None:
        return self._store.get(jid)

    async def update_status(
        self,
        jid: uuid.UUID,
        status: str,
        *,
        error_message: str | None = None,
        error_category: ErrorCategory | None = None,
        now: Any = None,
    ) -> _FakeJob:
        job = self._store[jid]
        if job.status == status:
            return job  # idempotent no-op (real semantics)
        if status not in JOB_TRANSITIONS.get(job.status, set()):
            msg = f"Invalid job status transition: {job.status!r} -> {status!r}"
            raise ValueError(msg)
        job.status = status
        if status in ("complete", "failed"):
            job.completed_at = "now"
            job.error_message = error_message
            job.error_category = error_category.value if error_category else None
        elif status in ("cancelled", "obsolete"):
            job.completed_at = "now"
        elif status == "active":
            job.started_at = "now"
        self._calls.append(("update_status", status))
        return job

    async def store_result(self, jid: uuid.UUID, result: dict[str, Any]) -> None:
        self._store[jid].result_data = result
        self._calls.append(("store_result", result))


class _FakeSession:
    def __init__(self, store: dict[uuid.UUID, _FakeJob], calls: list[Any]) -> None:
        self.store = store
        self.calls = calls

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def commit(self) -> None:
        self.calls.append(("commit",))

    async def rollback(self) -> None:
        self.calls.append(("rollback",))


def _ctx(store: dict[uuid.UUID, _FakeJob], calls: list[Any], **extra: Any) -> dict:
    def factory() -> _FakeSession:
        return _FakeSession(store, calls)

    return {"session_factory": factory, **extra}


@pytest.fixture(autouse=True)
def _use_fake_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seam, "JobRepository", _FakeRepo)


def _alive(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    async def _stub(*_a: Any, **_k: Any) -> bool:
        return value

    monkeypatch.setattr(seam, "_subject_is_alive", _stub)


# ── guard-lock + marker ──────────────────────────────────────────────────


def test_liveness_resolver_matches_subject_types() -> None:
    """Derive-or-verify (урок №18): the seam's liveness fetchers cover EXACTLY
    the non-NULL JOB_SUBJECT_TYPE tokens — no more, no less."""
    expected = {st for st in JOB_SUBJECT_TYPE.values() if st is not None}
    assert expected == seam._LIVENESS_SUBJECT_TYPES


def test_decorator_marks_wrapped() -> None:
    @through_seam()
    async def task(ctx: dict, job_id: str) -> None:  # pragma: no cover
        return None

    assert getattr(task, "__seam_wrapped__", False) is True


# ── entry: missing job (Рат.3 / DD-3.2.6-B) ──────────────────────────────


async def test_missing_job_retries_below_max() -> None:
    store: dict[uuid.UUID, _FakeJob] = {}  # empty → job missing
    calls: list[Any] = []

    @through_seam()
    async def task(ctx: dict, job_id: str) -> None:  # pragma: no cover
        raise AssertionError("body must not run when the job is missing")

    ctx = _ctx(store, calls, job_try=1)  # 1 < default max_tries (3)
    with pytest.raises(Retry):
        await task(ctx, str(uuid.uuid4()))


async def test_missing_job_gives_up_on_final_attempt() -> None:
    store: dict[uuid.UUID, _FakeJob] = {}
    calls: list[Any] = []
    ran = False

    @through_seam()
    async def task(ctx: dict, job_id: str) -> None:
        nonlocal ran
        ran = True  # pragma: no cover

    ctx = _ctx(store, calls, job_try=3)  # == default max_tries → give up
    await task(ctx, str(uuid.uuid4()))  # no raise, no body
    assert ran is False


# ── entry: terminal / cancelled skip (F1 crash-class fix) ────────────────


@pytest.mark.parametrize("status", ["cancelled", "complete", "failed", "obsolete"])
async def test_skip_when_already_terminal(status: str) -> None:
    jid = uuid.uuid4()
    store = {jid: _FakeJob(status)}
    calls: list[Any] = []
    ran = False

    @through_seam()
    async def task(ctx: dict, job_id: str) -> None:
        nonlocal ran
        ran = True  # pragma: no cover

    await task(_ctx(store, calls), str(jid))
    assert ran is False
    assert not any(c[0] == "update_status" for c in calls)  # no writes at all


# ── entry: liveness ──────────────────────────────────────────────────────


async def test_dead_subject_writes_obsolete_and_skips_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _alive(monkeypatch, False)
    jid = uuid.uuid4()
    store = {jid: _FakeJob("queued", "authored_document", uuid.uuid4())}
    calls: list[Any] = []
    ran = False

    @through_seam()
    async def task(ctx: dict, job_id: str) -> None:
        nonlocal ran
        ran = True  # pragma: no cover

    await task(_ctx(store, calls), str(jid))
    assert ran is False
    assert store[jid].status == "obsolete"
    assert store[jid].completed_at is not None
    assert store[jid].error_message is None


async def test_null_subject_skips_liveness_and_runs_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A3: s3_cleanup / legacy NULL-subject → liveness skipped, body runs.
    async def _boom(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("liveness must not be consulted for a NULL subject")

    monkeypatch.setattr(seam, "_subject_is_alive", _boom)
    jid = uuid.uuid4()
    store = {jid: _FakeJob("queued", None, None)}
    calls: list[Any] = []

    @through_seam()
    async def task(ctx: dict, job_id: str) -> dict:
        return {"deleted": [], "errors": []}

    await task(_ctx(store, calls), str(jid))
    assert store[jid].status == "complete"
    assert store[jid].result_data == {"deleted": [], "errors": []}


# ── exit: success / failure / result ─────────────────────────────────────


async def test_success_writes_complete_and_store_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _alive(monkeypatch, True)
    jid = uuid.uuid4()
    store = {jid: _FakeJob("queued", "project_base", uuid.uuid4())}
    calls: list[Any] = []

    @through_seam()
    async def task(ctx: dict, job_id: str) -> dict:
        return {"state": "ready"}

    await task(_ctx(store, calls), str(jid))
    assert store[jid].status == "complete"
    assert store[jid].started_at is not None
    assert store[jid].result_data == {"state": "ready"}


async def test_none_return_writes_complete_without_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _alive(monkeypatch, True)
    jid = uuid.uuid4()
    store = {jid: _FakeJob("queued", "course_node", uuid.uuid4())}
    calls: list[Any] = []

    @through_seam()
    async def task(ctx: dict, job_id: str) -> None:
        return None

    await task(_ctx(store, calls), str(jid))
    assert store[jid].status == "complete"
    assert store[jid].result_data is None


async def test_exception_writes_failed_with_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _alive(monkeypatch, True)
    jid = uuid.uuid4()
    store = {jid: _FakeJob("queued", "authored_document", uuid.uuid4())}
    calls: list[Any] = []

    class _CategorisedError(Exception):
        category = ErrorCategory.STAGE2_REJECTED

    @through_seam()
    async def task(ctx: dict, job_id: str) -> None:
        raise _CategorisedError("boom")

    await task(_ctx(store, calls), str(jid))  # swallowed (job is terminal)
    assert store[jid].status == "failed"
    assert store[jid].error_message == "boom"
    assert store[jid].error_category == ErrorCategory.STAGE2_REJECTED.value


# ── GO condition 1: arq.Retry is control flow, NOT a failure ─────────────


async def test_arq_retry_passes_through_without_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _alive(monkeypatch, True)
    jid = uuid.uuid4()
    store = {jid: _FakeJob("queued", "authored_document", uuid.uuid4())}
    calls: list[Any] = []

    @through_seam()
    async def task(ctx: dict, job_id: str) -> None:
        raise Retry(defer=1)

    with pytest.raises(Retry):
        await task(_ctx(store, calls), str(jid))
    # The job stays `active` — the seam did NOT terminalize it as failed.
    assert store[jid].status == "active"
    assert not any(c[0] == "update_status" and c[1] == "failed" for c in calls)


# ── replay: active on entry → body re-runs, active write no-op ───────────


async def test_replay_active_reruns_body_noop_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _alive(monkeypatch, True)
    jid = uuid.uuid4()
    job = _FakeJob("active", "course_node", uuid.uuid4())
    job.started_at = "original"
    store = {jid: job}
    calls: list[Any] = []
    ran = False

    @through_seam()
    async def task(ctx: dict, job_id: str) -> None:
        nonlocal ran
        ran = True

    await task(_ctx(store, calls), str(jid))
    assert ran is True  # active is in-flight, not at-rest → body runs
    assert store[jid].started_at == "original"  # active→active no-op preserved
    assert store[jid].status == "complete"


# ── opaque post-terminal callback (terminal-first, ingest) ───────────────


async def test_on_terminal_invoked_after_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _alive(monkeypatch, True)
    jid = uuid.uuid4()
    store = {jid: _FakeJob("queued", "authored_document", uuid.uuid4())}
    calls: list[Any] = []
    seen: list[SeamTerminal] = []
    status_at_callback: list[str] = []

    async def on_terminal(
        ctx: dict, job_id: str, *args: Any, outcome: SeamTerminal, **kw: Any
    ) -> None:
        seen.append(outcome)
        status_at_callback.append(store[jid].status)

    @through_seam(on_terminal=on_terminal)
    async def task(ctx: dict, job_id: str) -> None:
        return None

    await task(_ctx(store, calls), str(jid))
    assert len(seen) == 1
    assert seen[0].status == "complete"
    # Called AFTER the terminal write committed (terminal-first).
    assert status_at_callback == ["complete"]


async def test_on_terminal_skipped_when_terminal_write_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No terminal — no post-terminal: if a race moves the Job to cancelled
    mid-body, the complete write is skipped (ValueError) and the callback must
    NOT fire — otherwise the domain reacts to a terminal the DB does not hold."""
    _alive(monkeypatch, True)
    jid = uuid.uuid4()
    store = {jid: _FakeJob("queued", "authored_document", uuid.uuid4())}
    calls: list[Any] = []
    invoked = False

    async def on_terminal(
        ctx: dict, job_id: str, *args: Any, outcome: SeamTerminal, **kw: Any
    ) -> None:
        nonlocal invoked
        invoked = True  # pragma: no cover

    @through_seam(on_terminal=on_terminal)
    async def task(ctx: dict, job_id: str) -> None:
        # Simulate an external cancel landing while the body ran: the seam's
        # complete-write will hit cancelled→complete (illegal) and be skipped.
        store[jid].status = "cancelled"

    await task(_ctx(store, calls), str(jid))
    assert store[jid].status == "cancelled"  # complete write did NOT land
    assert invoked is False  # callback withheld


async def test_on_terminal_receives_task_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _alive(monkeypatch, True)
    jid = uuid.uuid4()
    store = {jid: _FakeJob("queued", "authored_document", uuid.uuid4())}
    calls: list[Any] = []
    received_args: list[Any] = []

    async def on_terminal(
        ctx: dict, job_id: str, material_id: str, *, outcome: SeamTerminal
    ) -> None:
        received_args.append(material_id)

    @through_seam(on_terminal=on_terminal)
    async def task(ctx: dict, job_id: str, material_id: str) -> None:
        return None

    await task(_ctx(store, calls), str(jid), "mat-123")
    assert received_args == ["mat-123"]
