"""Stage 2 dispatch logic in ``arq_ingest_material`` (Phase 2.1 C6).

Confirms KD-2.1-P contract on the task-body side:

* Stage 2 runs after ``process_raw`` and before Pass 2a.
* The ``safety_result`` row is committed regardless of outcome.
* On ``is_safe=False`` the task raises :class:`SecurityRejectedError`
  with ``ErrorCategory.STAGE2_REJECTED``; ``process_macro`` is NOT
  invoked and the callback gets the category discriminator.
* On ``is_safe=True`` control flows into Pass 2a (``process_macro``
  invoked once, then DocumentSummaryRepository.create).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.ingestion.schemas import DocumentSummaryDraft
from course_supporter.security.schemas import SafetyResult


def _safety_result(is_safe: bool, reasoning: str = "") -> SafetyResult:
    return SafetyResult(
        is_safe=is_safe,
        violations=[],
        confidence=0.95,
        reasoning=reasoning or ("benign" if is_safe else "off-topic content"),
    )


def _build_ctx(processor: MagicMock) -> dict[str, Any]:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    ctx_manager = AsyncMock()
    ctx_manager.__aenter__ = AsyncMock(return_value=session)
    ctx_manager.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=ctx_manager)

    return {
        "session_factory": factory,
        "model_router": MagicMock(),
        "stage_router": MagicMock(),
    }, processor


def _mock_doc() -> MagicMock:
    doc = MagicMock()
    doc.model_dump_json.return_value = '{"content": "ok"}'
    doc.metadata = {}
    chunk = MagicMock()
    chunk.text = "Materials about Python generators."
    doc.chunks = [chunk]
    return doc


def _patch_paths() -> dict[str, str]:
    return {
        "job_repo": "course_supporter.storage.job_repository.JobRepository",
        "entry_repo": (
            "course_supporter.storage.authored_document_repository"
            ".AuthoredDocumentRepository"
        ),
        "node_repo": (
            "course_supporter.storage.course_node_repository.CourseNodeRepository"
        ),
        "summary_repo": (
            "course_supporter.storage.document_summary_repository"
            ".DocumentSummaryRepository"
        ),
        "segment_repo": (
            "course_supporter.storage.document_segment_repository"
            ".DocumentSegmentRepository"
        ),
        "callback_cls": "course_supporter.ingestion_callback.IngestionCallback",
        "stage2": "course_supporter.security.stage2.run_stage2_safety_check",
        "check_work_window": "course_supporter.job_priority.check_work_window",
        "set_tenant_from_job": "course_supporter.api.tasks.set_tenant_from_job",
        "create_heavy": "course_supporter.api.tasks.create_heavy_steps",
        "create_processors": "course_supporter.api.tasks.create_processors",
    }


@pytest.mark.asyncio
async def test_stage2_pass_continues_to_pass_2a() -> None:
    """is_safe=True → safety_result committed; process_macro invoked once."""
    from course_supporter.api.tasks import arq_ingest_material

    jid = uuid.uuid4()
    mid = uuid.uuid4()
    doc = _mock_doc()

    processor = MagicMock()
    processor.process_raw = AsyncMock(return_value=doc)
    processor.process_macro = AsyncMock(
        return_value=DocumentSummaryDraft(
            title="Generators",
            description="Python generators primer",
            main_concepts=[],
            secondary_concepts=[],
        )
    )
    processor.process_detail = AsyncMock(return_value=[])

    ctx, _ = _build_ctx(processor)
    paths = _patch_paths()

    safe_result = _safety_result(is_safe=True)

    with (
        patch(paths["callback_cls"]) as cb_cls,
        patch(paths["check_work_window"]),
        patch(paths["job_repo"]) as job_cls,
        patch(paths["entry_repo"]) as entry_cls,
        patch(paths["node_repo"]) as node_cls,
        patch(paths["summary_repo"]) as summary_cls,
        patch(paths["segment_repo"]) as segment_cls,
        patch(paths["create_heavy"]),
        patch(paths["create_processors"], return_value={"web": processor}),
        patch(paths["set_tenant_from_job"], new=AsyncMock()),
        patch(paths["stage2"], new=AsyncMock(return_value=safe_result)) as mock_stage2,
    ):
        mock_entry = MagicMock()
        mock_entry.id = mid
        mock_entry.source_url = "https://example.com"
        mock_entry.language = "en"
        mock_entry.course_node_id = uuid.uuid4()

        job_cls.return_value.update_status = AsyncMock()
        job_cls.return_value.update_stage = AsyncMock()
        entry_cls.return_value.get_by_id = AsyncMock(return_value=mock_entry)
        entry_cls.return_value.set_pending = AsyncMock()
        entry_cls.return_value.store_safety_result = AsyncMock()
        node_cls.return_value.get_root_for = AsyncMock(
            return_value=MagicMock(title="Course", description="Desc")
        )
        node_cls.return_value.get_by_id = AsyncMock(
            return_value=MagicMock(title="Node", description="Node desc")
        )
        summary_cls.return_value.create = AsyncMock(
            return_value=MagicMock(id=uuid.uuid4()),
        )
        segment_cls.return_value.create_batch = AsyncMock(return_value=[])
        cb_cls.return_value.on_success = AsyncMock()
        cb_cls.return_value.on_failure = AsyncMock()

        await arq_ingest_material(ctx, str(jid), str(mid), "web", "https://example.com")

    mock_stage2.assert_awaited_once()
    entry_cls.return_value.store_safety_result.assert_awaited_once()
    sr_kwargs = entry_cls.return_value.store_safety_result.call_args.kwargs
    assert sr_kwargs["safety_result"]["is_safe"] is True
    processor.process_macro.assert_awaited_once()
    processor.process_detail.assert_awaited_once()
    segment_cls.return_value.create_batch.assert_awaited_once()
    cb_cls.return_value.on_success.assert_awaited_once()
    cb_cls.return_value.on_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_stage2_reject_skips_pass_2a() -> None:
    """is_safe=False → safety_result committed; process_macro NOT invoked."""
    from course_supporter.api.tasks import arq_ingest_material

    jid = uuid.uuid4()
    mid = uuid.uuid4()
    doc = _mock_doc()

    processor = MagicMock()
    processor.process_raw = AsyncMock(return_value=doc)
    processor.process_macro = AsyncMock()  # should NOT be called

    ctx, _ = _build_ctx(processor)
    paths = _patch_paths()

    reject_result = _safety_result(
        is_safe=False, reasoning="content is off-topic relative to course"
    )

    with (
        patch(paths["callback_cls"]) as cb_cls,
        patch(paths["check_work_window"]),
        patch(paths["job_repo"]) as job_cls,
        patch(paths["entry_repo"]) as entry_cls,
        patch(paths["node_repo"]) as node_cls,
        patch(paths["summary_repo"]) as summary_cls,
        patch(paths["create_heavy"]),
        patch(paths["create_processors"], return_value={"web": processor}),
        patch(paths["set_tenant_from_job"], new=AsyncMock()),
        patch(paths["stage2"], new=AsyncMock(return_value=reject_result)),
    ):
        mock_entry = MagicMock()
        mock_entry.id = mid
        mock_entry.source_url = "https://example.com"
        mock_entry.language = "en"
        mock_entry.course_node_id = uuid.uuid4()

        job_cls.return_value.update_status = AsyncMock()
        job_cls.return_value.update_stage = AsyncMock()
        entry_cls.return_value.get_by_id = AsyncMock(return_value=mock_entry)
        entry_cls.return_value.set_pending = AsyncMock()
        entry_cls.return_value.store_safety_result = AsyncMock()
        node_cls.return_value.get_root_for = AsyncMock(
            return_value=MagicMock(title="Course", description="Desc")
        )
        node_cls.return_value.get_by_id = AsyncMock(
            return_value=MagicMock(title="Node", description="Node desc")
        )
        summary_cls.return_value.create = AsyncMock()
        cb_cls.return_value.on_success = AsyncMock()
        cb_cls.return_value.on_failure = AsyncMock()

        await arq_ingest_material(ctx, str(jid), str(mid), "web", "https://example.com")

    entry_cls.return_value.store_safety_result.assert_awaited_once()
    sr_kwargs = entry_cls.return_value.store_safety_result.call_args.kwargs
    assert sr_kwargs["safety_result"]["is_safe"] is False
    processor.process_macro.assert_not_awaited()
    summary_cls.return_value.create.assert_not_awaited()
    cb_cls.return_value.on_failure.assert_awaited_once()

    from course_supporter.security.exceptions import ErrorCategory

    failure_kwargs = cb_cls.return_value.on_failure.call_args.kwargs
    assert failure_kwargs["error_category"] is ErrorCategory.STAGE2_REJECTED
    cb_cls.return_value.on_success.assert_not_awaited()


@pytest.mark.asyncio
async def test_stage2_pass_order_is_checking_safety_then_extracting() -> None:
    """Job.current_stage transitions per KD-2.1-B taxonomy ordering."""
    from course_supporter.api.tasks import arq_ingest_material

    jid = uuid.uuid4()
    mid = uuid.uuid4()
    doc = _mock_doc()

    processor = MagicMock()
    processor.process_raw = AsyncMock(return_value=doc)
    processor.process_macro = AsyncMock(
        return_value=DocumentSummaryDraft(
            title="t",
            description="d",
            main_concepts=[],
            secondary_concepts=[],
        )
    )
    processor.process_detail = AsyncMock(return_value=[])

    ctx, _ = _build_ctx(processor)
    paths = _patch_paths()
    safe_result = _safety_result(is_safe=True)

    with (
        patch(paths["callback_cls"]) as cb_cls,
        patch(paths["check_work_window"]),
        patch(paths["job_repo"]) as job_cls,
        patch(paths["entry_repo"]) as entry_cls,
        patch(paths["node_repo"]) as node_cls,
        patch(paths["summary_repo"]) as summary_cls,
        patch(paths["segment_repo"]) as segment_cls,
        patch(paths["create_heavy"]),
        patch(paths["create_processors"], return_value={"web": processor}),
        patch(paths["set_tenant_from_job"], new=AsyncMock()),
        patch(paths["stage2"], new=AsyncMock(return_value=safe_result)),
    ):
        mock_entry = MagicMock()
        mock_entry.id = mid
        mock_entry.source_url = "https://example.com"
        mock_entry.language = "en"
        mock_entry.course_node_id = uuid.uuid4()

        job_cls.return_value.update_status = AsyncMock()
        job_cls.return_value.update_stage = AsyncMock()
        entry_cls.return_value.get_by_id = AsyncMock(return_value=mock_entry)
        entry_cls.return_value.set_pending = AsyncMock()
        entry_cls.return_value.store_safety_result = AsyncMock()
        node_cls.return_value.get_root_for = AsyncMock(
            return_value=MagicMock(title="t", description="d")
        )
        node_cls.return_value.get_by_id = AsyncMock(
            return_value=MagicMock(title="t", description="d")
        )
        summary_cls.return_value.create = AsyncMock(
            return_value=MagicMock(id=uuid.uuid4())
        )
        segment_cls.return_value.create_batch = AsyncMock(return_value=[])
        cb_cls.return_value.on_success = AsyncMock()
        cb_cls.return_value.on_failure = AsyncMock()

        await arq_ingest_material(ctx, str(jid), str(mid), "web", "https://example.com")

    stage_calls = [
        call.args[1] for call in job_cls.return_value.update_stage.await_args_list
    ]
    assert stage_calls == [
        "checking_safety",
        "extracting_structure",
        "creating_segments",
    ]
