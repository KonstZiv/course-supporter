"""Integration tests for CriteriaListService (mentor-rebuild task 08, K3).

Requires ``docker compose up -d`` (PostgreSQL).
Run with ``uv run pytest --run-db`` against this file.

The claim protocol of ``TASK.md`` section 9, decision 2, on a real database
and with committed rows — two submissions only see each other's commits:

* a list stored before its caller fails stays, and the next call does not pay;
* an empty answer at the first rung's output ceiling fails the composition with
  its reason, and the second rung is never called;
* two submissions at once make one model call, and the second takes the list;
* an abandoned claim is taken over exactly once, and the takeover is logged;
* when the first composition fails, the second submission gets no list and
  makes no call of its own;
* a list composed without concepts is composed again exactly once, when a
  summary appears.

The composer is a double except in the ceiling test, which runs the real agent
on the real router with doubled providers.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from course_supporter.agents.criteria_decomposer import (
    STAGE_NAME,
    CriteriaDecomposerAgent,
)
from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.criteria_list_state import CriteriaListState
from course_supporter.homework.criteria_form import (
    CriteriaComposition,
    CriterionDraft,
    compose_criteria,
)
from course_supporter.homework.criteria_list_service import (
    CriteriaInForce,
    CriteriaListService,
    CriteriaUnavailable,
    UnavailableReason,
    input_fingerprint,
    load_in_force,
)
from course_supporter.llm.error_categories import ErrorCategory, LadderExhaustedError
from course_supporter.llm.finish_reason import FinishReason
from course_supporter.llm.ladder_config import load_ladder_config
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.registry import load_registry
from course_supporter.llm.schemas import LLMResponse
from course_supporter.llm.stage_router import StageRouter
from course_supporter.storage.document_summary_repository import (
    DocumentSummaryRepository,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
    NodeSummaryFinal,
    TaskCriteriaList,
    Tenant,
)
from course_supporter.storage.task_criteria_list_repository import (
    TaskCriteriaListRepository,
)
from course_supporter.storage.task_criteria_override_repository import (
    TaskCriteriaOverrideRepository,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_TASK_TEXT = "Write a recursive factorial function and handle n = 0."
_PROMPT_HASH = "p" * 64
_QUICK_WAIT = timedelta(milliseconds=400)
# The heartbeat scaled down: a beat every 50 ms, a claim abandoned after 300 ms
# of silence — six beats, as production's five in five minutes.
_BEAT = timedelta(milliseconds=50)
_SILENCE = timedelta(milliseconds=300)


# ── Seeds ───────────────────────────────────────────────────────────


@pytest.fixture()
async def course(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """A course root, a node under it, and an ingested task on the node."""
    async with session_factory() as session:
        tenant = Tenant(name=f"criteria-list-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        root = make_root_course_node(tenant_id=tenant.id, title="Python", order=0)
        session.add(root)
        await session.flush()
        node = CourseNode(
            tenant_id=tenant.id, title="Recursion", order=0, parent_id=root.id
        )
        session.add(node)
        await session.flush()
        task = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=root.id,
            source_type="web",
            source_url="https://example.com/task",
            task_type="task",
            language="ukr",
        )
        session.add(task)
        await session.flush()
        summary = await DocumentSummaryRepository(session).create(
            authored_document_id=task.id,
            title="The Task",
            description="Task description.",
            main_concepts=[],
            secondary_concepts=[],
            content_char_count=len(_TASK_TEXT),
        )
        session.add(
            DocumentSegment(
                document_summary_id=summary.id,
                course_root_id=summary.course_root_id,
                order=0,
                start_pos=0,
                end_pos=len(_TASK_TEXT),
                content=_TASK_TEXT,
            )
        )
        await session.commit()
        ids = {"tenant": tenant.id, "root": root.id, "node": node.id, "task": task.id}

    yield ids

    async with session_factory() as session:
        # Lists, edits, summaries and segments go with their task (FK CASCADE);
        # node summaries go with their node.
        await session.execute(
            delete(AuthoredDocument).where(
                AuthoredDocument.course_root_id == ids["root"]
            )
        )
        await session.execute(delete(CourseNode).where(CourseNode.id == ids["node"]))
        await session.execute(delete(CourseNode).where(CourseNode.id == ids["root"]))
        await session.execute(delete(Tenant).where(Tenant.id == ids["tenant"]))
        await session.commit()


async def _add_final(
    session_factory: async_sessionmaker[AsyncSession],
    course_node_id: uuid.UUID,
    **fields: Any,
) -> None:
    async with session_factory() as session:
        session.add(NodeSummaryFinal(course_node_id=course_node_id, **fields))
        await session.commit()


async def _remove_finals(
    session_factory: async_sessionmaker[AsyncSession], course_node_id: uuid.UUID
) -> None:
    async with session_factory() as session:
        await session.execute(
            delete(NodeSummaryFinal).where(
                NodeSummaryFinal.course_node_id == course_node_id
            )
        )
        await session.commit()


async def _document(
    session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID
) -> AuthoredDocument:
    async with session_factory() as session:
        document = await session.get(AuthoredDocument, task_id)
        assert document is not None
        return document


async def _rows(
    session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID
) -> list[TaskCriteriaList]:
    async with session_factory() as session:
        result = await session.execute(
            select(TaskCriteriaList)
            .where(TaskCriteriaList.authored_document_id == task_id)
            .order_by(TaskCriteriaList.created_at, TaskCriteriaList.id)
        )
        return list(result.scalars())


async def _pending_claim(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    *,
    age: timedelta = timedelta(0),
) -> TaskCriteriaList:
    """A claim nobody is working on, made ``age`` ago."""
    document = await _document(session_factory, task_id)
    async with session_factory() as session:
        repo = TaskCriteriaListRepository(session)
        row = await repo.claim(
            authored_document_id=task_id,
            source_content_hash=document.content_hash or "",
            source_task_type=document.task_type or "",
            form_version=2,
        )
        assert row is not None
        if age:
            claimed_at = await repo.take_over(
                row.id,
                seen_claimed_at=row.claimed_at,
                now=datetime.now(UTC) - age,
            )
            assert claimed_at is not None
        await session.commit()
    [claim] = [r for r in await _rows(session_factory, task_id) if r.id == row.id]
    return claim


# ── Doubles ─────────────────────────────────────────────────────────


class _FakeComposer:
    """Counts calls and answers with one criterion per draft."""

    def __init__(
        self,
        *,
        delay: float = 0.0,
        error: BaseException | None = None,
        drafts: list[CriterionDraft] | None = None,
    ) -> None:
        self.calls = 0
        self.last: dict[str, Any] | None = None
        self._delay = delay
        self._error = error
        self._drafts = drafts or [
            CriterionDraft(
                text="Has a base case",
                evidence="an explicit if-return for n = 0",
                weight="must",
                check_method="model_verdict",
                concepts=("Recursion", "Monads"),
            )
        ]

    async def compose(self, **kwargs: Any) -> CriteriaComposition:
        self.calls += 1
        self.last = kwargs
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return compose_criteria(
            self._drafts,
            ["The node says recursion is not covered."],
            input_concepts=[*kwargs["node_concepts"], *kwargs["root_concepts"]],
        )

    def prompt_hash(self) -> str:
        return _PROMPT_HASH


def _service(
    session_factory: async_sessionmaker[AsyncSession],
    composer: Any,
    *,
    abandoned_after: timedelta = timedelta(hours=1),
    wait_limit: timedelta = timedelta(seconds=10),
) -> CriteriaListService:
    return CriteriaListService(
        session_factory,
        composer,
        abandoned_after=abandoned_after,
        heartbeat_interval=_BEAT,
        wait_limit=wait_limit,
        poll_interval=timedelta(milliseconds=20),
    )


def _meet_at(monkeypatch: pytest.MonkeyPatch, method: str, parties: int = 2) -> None:
    """Hold every call of a repository method until ``parties`` have arrived.

    Two submissions then reach the same step having read the same state — the
    race the protocol exists for, made certain instead of likely.
    """
    barrier = asyncio.Barrier(parties)
    original = getattr(TaskCriteriaListRepository, method)

    async def meet(self: TaskCriteriaListRepository, *args: Any, **kwargs: Any) -> Any:
        await barrier.wait()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(TaskCriteriaListRepository, method, meet)


async def _together(*calls: Any) -> list[Any]:
    # A bound, so a broken protocol fails the test instead of hanging the gate.
    return list(await asyncio.wait_for(asyncio.gather(*calls), timeout=30))


# ── The locks of section 5 ──────────────────────────────────────────


class TestComposedOnce:
    async def test_a_miss_composes_and_stores_a_ready_list(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        composer = _FakeComposer()

        result = await _service(session_factory, composer).get_or_compose(
            course["task"]
        )

        assert isinstance(result, CriteriaInForce)
        assert result.layer is CriteriaLayer.MODEL
        assert [c.id for c in result.criteria] == ["c1"]
        [row] = await _rows(session_factory, course["task"])
        assert row.id == result.source_id
        assert row.state == CriteriaListState.READY.value
        assert row.deleted_at is None
        assert row.criteria is not None and row.criteria[0]["id"] == "c1"
        assert row.form_version == 2
        assert row.contradictions == ["The node says recursion is not covered."]
        # No summary anywhere: no concepts in the input, both named ones dropped.
        assert row.concepts_in_input is False
        assert row.dropped_concept_count == 2
        assert composer.calls == 1

    async def test_a_list_stored_before_its_caller_fails_stays(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        composer = _FakeComposer()
        service = _service(session_factory, composer)

        async def review_that_fails() -> None:
            # The review's own transaction: it fails after the list is in and
            # takes nothing of the list with it.
            async with session_factory() as review_session:
                await review_session.get(AuthoredDocument, course["task"])
                await service.get_or_compose(course["task"])
                raise RuntimeError("the review failed after the list")

        with pytest.raises(RuntimeError, match="after the list"):
            await review_that_fails()
        again = await service.get_or_compose(course["task"])

        assert composer.calls == 1
        assert isinstance(again, CriteriaInForce)
        [row] = await _rows(session_factory, course["task"])
        assert again.source_id == row.id

    async def test_two_submissions_at_once_make_one_call(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        composer = _FakeComposer(delay=0.2)
        service = _service(session_factory, composer)
        _meet_at(monkeypatch, "claim")

        first, second = await _together(
            service.get_or_compose(course["task"]),
            service.get_or_compose(course["task"]),
        )

        assert composer.calls == 1
        assert isinstance(first, CriteriaInForce)
        assert isinstance(second, CriteriaInForce)
        assert first.source_id == second.source_id
        assert first.criteria == second.criteria
        assert len(await _rows(session_factory, course["task"])) == 1


class TestFailure:
    async def test_an_empty_answer_at_the_first_ceiling_stops_the_ladder(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        """The real agent and router; only the providers are doubles."""
        ladders = load_ladder_config(Path("config"))
        stage = ladders.get_stage(STAGE_NAME).model_copy(
            # The shipped ladder, with the v2 prompt the agent's answer is
            # checked against (the ladder itself switches to v2 in K4).
            update={"prompt_ref": "prompts/criteria_decomposition/v2.md"}
        )
        empty = _provider("", FinishReason.OUTPUT_CEILING, tokens_out=32768)
        answering = _provider(
            '{"criteria": [{"text": "t", "evidence": "e", "weight": "must",'
            ' "check_method": "model_verdict"}]}',
            FinishReason.STOP,
            tokens_out=300,
        )
        router = StageRouter(
            ladders,
            {"deepseek_thinking": empty, "dashscope": answering, "deepseek": answering},
            registry=load_registry(Path("config/external_services.yaml")),
        )
        service = _service(
            session_factory, CriteriaDecomposerAgent(router, stage=stage)
        )

        result = await service.get_or_compose(course["task"])

        assert result == CriteriaUnavailable(UnavailableReason.COMPOSITION_FAILED)
        empty.complete.assert_awaited_once()
        answering.complete.assert_not_awaited()
        [row] = await _rows(session_factory, course["task"])
        assert row.state == CriteriaListState.FAILED.value
        assert row.deleted_at is not None
        assert row.failure_reason is not None
        assert row.failure_reason.startswith("output_ceiling: ")

    async def test_when_the_first_fails_the_second_gets_no_list_and_no_call(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        composer = _FakeComposer(
            delay=0.2, error=LadderExhaustedError(STAGE_NAME, [("p", "m", "boom")])
        )
        service = _service(session_factory, composer)
        _meet_at(monkeypatch, "claim")

        results = await _together(
            service.get_or_compose(course["task"]),
            service.get_or_compose(course["task"]),
        )

        assert composer.calls == 1
        assert results == [
            CriteriaUnavailable(UnavailableReason.COMPOSITION_FAILED),
            CriteriaUnavailable(UnavailableReason.COMPOSITION_FAILED),
        ]
        [row] = await _rows(session_factory, course["task"])
        assert row.state == CriteriaListState.FAILED.value
        assert row.failure_reason is not None
        assert row.failure_reason.startswith("exhausted: ")

    async def test_a_failed_composition_frees_the_version_for_the_next(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        failing = _FakeComposer(error=LadderExhaustedError(STAGE_NAME, []))
        await _service(session_factory, failing).get_or_compose(course["task"])

        composer = _FakeComposer()
        result = await _service(session_factory, composer).get_or_compose(
            course["task"]
        )

        assert composer.calls == 1
        assert isinstance(result, CriteriaInForce)

    async def test_a_defect_in_composing_is_recorded_and_raised(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        composer = _FakeComposer(error=RuntimeError("a defect"))

        with pytest.raises(RuntimeError, match="a defect"):
            await _service(session_factory, composer).get_or_compose(course["task"])

        # Not left pending until the claim counts as abandoned.
        [row] = await _rows(session_factory, course["task"])
        assert row.state == CriteriaListState.FAILED.value
        assert row.failure_reason == "error: RuntimeError"


def _provider(content: str, finish: FinishReason, *, tokens_out: int) -> AsyncMock:
    provider = AsyncMock(spec=LLMProvider)
    provider.enabled = True
    provider.complete = AsyncMock(
        return_value=LLMResponse(
            content=content,
            provider="double",
            model_id="double",
            tokens_in=900,
            tokens_out=tokens_out,
            finish_reason=finish,
        )
    )
    provider.classify_error = lambda _exc: ErrorCategory.SEMANTIC
    return provider


class TestAbandonedClaim:
    async def test_an_abandoned_claim_is_taken_over_exactly_once(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        abandoned = await _pending_claim(
            session_factory, course["task"], age=timedelta(hours=2)
        )
        composer = _FakeComposer(delay=0.2)
        service = _service(
            session_factory, composer, abandoned_after=timedelta(hours=1)
        )
        _meet_at(monkeypatch, "take_over")

        with capture_logs() as logs:
            first, second = await _together(
                service.get_or_compose(course["task"]),
                service.get_or_compose(course["task"]),
            )

        takeovers = [e for e in logs if e["event"] == "criteria_list_claim_taken_over"]
        assert len(takeovers) == 1
        assert takeovers[0]["list_id"] == str(abandoned.id)
        assert composer.calls == 1
        assert isinstance(first, CriteriaInForce)
        assert isinstance(second, CriteriaInForce)
        assert first.source_id == second.source_id == abandoned.id
        [row] = await _rows(session_factory, course["task"])
        assert row.state == CriteriaListState.READY.value

    async def test_a_recent_claim_is_waited_for_not_taken_over(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        claim = await _pending_claim(session_factory, course["task"])
        composer = _FakeComposer()
        service = _service(
            session_factory,
            composer,
            abandoned_after=timedelta(hours=1),
            wait_limit=_QUICK_WAIT,
        )

        result = await service.get_or_compose(course["task"])

        assert result == CriteriaUnavailable(UnavailableReason.WAIT_EXHAUSTED)
        assert composer.calls == 0
        [row] = await _rows(session_factory, course["task"])
        assert row.claimed_at == claim.claimed_at
        assert row.state == CriteriaListState.PENDING.value

    async def test_a_claimer_that_lost_its_claim_writes_nothing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        class _OvertakenComposer(_FakeComposer):
            async def compose(self, **kwargs: Any) -> CriteriaComposition:
                # Another submission takes the claim over mid-call, and the
                # model goes on answering for a few more heartbeats.
                async with session_factory() as session:
                    repo = TaskCriteriaListRepository(session)
                    live = await repo.get_live(course["task"])
                    assert live is not None
                    await repo.take_over(live.id, seen_claimed_at=live.claimed_at)
                    await session.commit()
                await asyncio.sleep(3 * _BEAT.total_seconds())
                return await super().compose(**kwargs)

        service = _service(
            session_factory, _OvertakenComposer(), wait_limit=_QUICK_WAIT
        )

        with capture_logs() as logs:
            result = await service.get_or_compose(course["task"])

        assert result == CriteriaUnavailable(UnavailableReason.WAIT_EXHAUSTED)
        events = [e["event"] for e in logs]
        # The heartbeat found the claim gone once and stopped beating.
        assert events.count("criteria_list_heartbeat_lost") == 1
        assert "criteria_list_write_refused" in events
        [row] = await _rows(session_factory, course["task"])
        assert row.state == CriteriaListState.PENDING.value
        assert row.criteria is None


class TestHeartbeat:
    """The claimer's heartbeat (section 9, decision 2 as refined on 2026-09-30)."""

    async def test_a_long_composition_is_not_taken_over_while_it_beats(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        composer = _FakeComposer(delay=1.0)  # far longer than the silence limit
        service = _service(session_factory, composer, abandoned_after=_SILENCE)

        with capture_logs() as logs:
            first = asyncio.create_task(service.get_or_compose(course["task"]))
            await asyncio.sleep(2 * _SILENCE.total_seconds())
            second = await asyncio.wait_for(
                service.get_or_compose(course["task"]), timeout=30
            )
            first_result = await asyncio.wait_for(first, timeout=30)

        assert composer.calls == 1
        assert isinstance(first_result, CriteriaInForce)
        assert isinstance(second, CriteriaInForce)
        assert second.source_id == first_result.source_id
        assert not [e for e in logs if e["event"] == "criteria_list_claim_taken_over"]
        # The premise: the claim outlived the silence limit, and only the
        # heartbeat's renewals kept it alive.
        [row] = await _rows(session_factory, course["task"])
        assert row.claimed_at - row.created_at > _SILENCE

    async def test_a_cancelled_composition_is_taken_over_once_it_falls_silent(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        slow = _FakeComposer(delay=30.0)
        first = asyncio.create_task(
            _service(session_factory, slow, abandoned_after=_SILENCE).get_or_compose(
                course["task"]
            )
        )
        await asyncio.sleep(4 * _BEAT.total_seconds())
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        [claimed] = await _rows(session_factory, course["task"])
        await asyncio.sleep(_SILENCE.total_seconds() + 4 * _BEAT.total_seconds())
        [silent] = await _rows(session_factory, course["task"])
        fast = _FakeComposer()

        with capture_logs() as logs:
            result = await _service(
                session_factory, fast, abandoned_after=_SILENCE
            ).get_or_compose(course["task"])

        # A cancelled job is not recorded as failed, and its heartbeat stopped
        # with it: the claim fell silent.
        assert claimed.state == CriteriaListState.PENDING.value
        assert silent.claimed_at == claimed.claimed_at
        assert isinstance(result, CriteriaInForce)
        assert result.source_id == claimed.id
        assert (slow.calls, fast.calls) == (1, 1)
        takeovers = [e for e in logs if e["event"] == "criteria_list_claim_taken_over"]
        assert len(takeovers) == 1

    async def test_after_the_list_is_ready_the_heartbeat_writes_no_more(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        loop = asyncio.get_running_loop()
        renewals: list[float] = []
        renew = TaskCriteriaListRepository.renew

        async def counted(
            self: TaskCriteriaListRepository, *args: Any, **kwargs: Any
        ) -> Any:
            renewals.append(loop.time())
            return await renew(self, *args, **kwargs)

        monkeypatch.setattr(TaskCriteriaListRepository, "renew", counted)
        composer = _FakeComposer(delay=6 * _BEAT.total_seconds())

        result = await _service(session_factory, composer).get_or_compose(
            course["task"]
        )
        ready_at = loop.time()
        await asyncio.sleep(6 * _BEAT.total_seconds())

        assert isinstance(result, CriteriaInForce)
        assert renewals, "the premise: the heartbeat beat while the model answered"
        assert all(at <= ready_at for at in renewals)


class TestRecomposedOnce:
    async def test_a_list_without_concepts_is_recomposed_once_they_appear(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        composer = _FakeComposer()
        service = _service(session_factory, composer)
        first = await service.get_or_compose(course["task"])
        assert isinstance(first, CriteriaInForce)

        await _add_final(session_factory, course["node"], main_concepts=["Recursion"])
        second = await service.get_or_compose(course["task"])
        third = await service.get_or_compose(course["task"])
        await _remove_finals(session_factory, course["node"])
        fourth = await service.get_or_compose(course["task"])

        assert composer.calls == 2
        assert composer.last is not None
        assert composer.last["node_concepts"] == ["Recursion"]
        assert isinstance(second, CriteriaInForce)
        assert second.source_id != first.source_id
        assert second.criteria[0].concepts == ("Recursion",)
        assert isinstance(third, CriteriaInForce)
        assert isinstance(fourth, CriteriaInForce)
        assert third.source_id == fourth.source_id == second.source_id
        old, new = await _rows(session_factory, course["task"])
        assert old.id == first.source_id and old.deleted_at is not None
        assert new.concepts_in_input is True

    async def test_a_list_composed_with_concepts_is_not_recomposed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        await _add_final(session_factory, course["root"], main_concepts=["Functions"])
        composer = _FakeComposer()
        service = _service(session_factory, composer)

        await service.get_or_compose(course["task"])
        await _add_final(session_factory, course["node"], main_concepts=["Recursion"])
        await service.get_or_compose(course["task"])

        assert composer.calls == 1


class TestVersionAndLayers:
    async def test_a_new_version_of_the_task_is_composed_anew(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        composer = _FakeComposer()
        service = _service(session_factory, composer)
        first = await service.get_or_compose(course["task"])
        async with session_factory() as session:
            document = await session.get(AuthoredDocument, course["task"])
            assert document is not None
            document.task_type = "project"
            await session.commit()

        second = await service.get_or_compose(course["task"])

        assert composer.calls == 2
        assert isinstance(first, CriteriaInForce)
        assert isinstance(second, CriteriaInForce)
        old, new = await _rows(session_factory, course["task"])
        assert old.deleted_at is not None
        assert new.source_task_type == "project"
        assert new.id == second.source_id

    async def test_the_authors_edit_for_this_version_is_in_force(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        document = await _document(session_factory, course["task"])
        edit = await _replace_edit(session_factory, document)
        composer = _FakeComposer()

        result = await _service(session_factory, composer).get_or_compose(
            course["task"]
        )

        assert composer.calls == 0
        assert isinstance(result, CriteriaInForce)
        assert result.layer is CriteriaLayer.AUTHOR
        assert result.source_id == edit
        async with session_factory() as session:
            read = await load_in_force(session, course["task"])
        assert read == result

    async def test_an_edit_of_an_earlier_version_is_not_in_force(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        document = await _document(session_factory, course["task"])
        await _replace_edit(session_factory, document, content_hash="0" * 64)
        composer = _FakeComposer()

        result = await _service(session_factory, composer).get_or_compose(
            course["task"]
        )

        assert composer.calls == 1
        assert isinstance(result, CriteriaInForce)
        assert result.layer is CriteriaLayer.MODEL

    async def test_a_task_that_is_not_ingested_has_no_list(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        async with session_factory() as session:
            bare = AuthoredDocument(
                course_node_id=course["node"],
                course_root_id=course["root"],
                source_type="web",
                source_url="https://example.com/bare",
                task_type="task",
            )
            session.add(bare)
            await session.commit()
        composer = _FakeComposer()

        result = await _service(session_factory, composer).get_or_compose(bare.id)

        assert result == CriteriaUnavailable(UnavailableReason.TASK_NOT_READY)
        assert composer.calls == 0

    async def test_a_task_whose_summary_is_not_ready_has_no_list(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        # Being processed again: the task keeps the content hash of its last
        # version while its summary is no longer the ready one.
        async with session_factory() as session:
            summary = await session.scalar(
                select(DocumentSummary).where(
                    DocumentSummary.authored_document_id == course["task"],
                    DocumentSummary.deleted_at.is_(None),
                )
            )
            assert summary is not None
            summary.deleted_at = datetime.now(UTC)
            await session.commit()
        document = await _document(session_factory, course["task"])
        # The premise: a version key is there, so only the summary guard can
        # say "not ready".
        assert document.content_hash is not None
        assert document.task_type is not None
        composer = _FakeComposer()

        result = await _service(session_factory, composer).get_or_compose(
            course["task"]
        )

        assert result == CriteriaUnavailable(UnavailableReason.TASK_NOT_READY)
        assert composer.calls == 0
        assert await _rows(session_factory, course["task"]) == []


async def _replace_edit(
    session_factory: async_sessionmaker[AsyncSession],
    document: AuthoredDocument,
    *,
    content_hash: str | None = None,
) -> uuid.UUID:
    async with session_factory() as session:
        edit = await TaskCriteriaOverrideRepository(session).replace(
            authored_document_id=document.id,
            source_content_hash=content_hash or document.content_hash or "",
            source_task_type=document.task_type or "",
            criteria=[
                {
                    "id": "c1",
                    "text": "The author's criterion",
                    "evidence": "as the author wrote it",
                    "weight": "should",
                    "check_method": "model_verdict",
                    "soft_descent": False,
                    "concepts": [],
                    "mandatory_points": [],
                }
            ],
        )
        await session.commit()
        return edit.id


class TestInput:
    async def test_the_model_reads_the_task_in_its_course(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        await _add_final(
            session_factory,
            course["node"],
            description="Recursion in Python.",
            main_concepts=["Recursion", "Base Case"],
        )
        await _add_final(session_factory, course["root"], main_concepts=["Functions"])
        composer = _FakeComposer()

        await _service(session_factory, composer).get_or_compose(course["task"])

        assert composer.last == {
            "task_title": "The Task",
            "task_description": "Task description.",
            "task_text": _TASK_TEXT,
            "task_type": "task",
            "language": "Ukrainian",
            "node_description": "Recursion in Python.",
            "node_concepts": ["Recursion", "Base Case"],
            "root_concepts": ["Functions"],
        }
        document = await _document(session_factory, course["task"])
        [row] = await _rows(session_factory, course["task"])
        assert row.concepts_in_input is True
        assert row.input_fingerprint == input_fingerprint(
            content_hash=document.content_hash or "",
            node_description="Recursion in Python.",
            node_concepts=["Recursion", "Base Case"],
            root_concepts=["Functions"],
            prompt_hash=_PROMPT_HASH,
        )

    async def test_a_task_on_the_root_reads_one_summary_not_two(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        async with session_factory() as session:
            task = AuthoredDocument(
                course_node_id=course["root"],
                course_root_id=course["root"],
                source_type="web",
                source_url="https://example.com/root-task",
                task_type="task",
            )
            session.add(task)
            await session.flush()
            summary = await DocumentSummaryRepository(session).create(
                authored_document_id=task.id,
                title="A task of the whole course",
                description="",
                main_concepts=[],
                secondary_concepts=[],
                content_char_count=4,
            )
            session.add(
                DocumentSegment(
                    document_summary_id=summary.id,
                    course_root_id=summary.course_root_id,
                    order=0,
                    start_pos=0,
                    end_pos=4,
                    content="text",
                )
            )
            await session.commit()
            task_id = task.id
        await _add_final(
            session_factory,
            course["root"],
            description="The course.",
            main_concepts=["Functions"],
        )
        composer = _FakeComposer()

        await _service(session_factory, composer).get_or_compose(task_id)

        assert composer.last is not None
        assert composer.last["node_description"] == "The course."
        assert composer.last["node_concepts"] == ["Functions"]
        assert composer.last["root_concepts"] == []

    async def test_an_authors_empty_module_gives_its_own_description(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: dict[str, uuid.UUID],
    ) -> None:
        await _add_final(
            session_factory,
            course["node"],
            description="The methodist's words.",
            is_manual=True,
            manual_description="The author's words.",
        )
        composer = _FakeComposer()

        await _service(session_factory, composer).get_or_compose(course["task"])

        assert composer.last is not None
        assert composer.last["node_description"] == "The author's words."
