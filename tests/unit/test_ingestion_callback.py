"""Tests for IngestionCallback service."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.ingestion_callback import IngestionCallback

# Patch targets — imports inside ingestion_callback functions
_ENTRY_REPO = (
    "course_supporter.storage.authored_document_repository.AuthoredDocumentRepository"
)
_JOB_REPO = "course_supporter.ingestion_callback.JobRepository"


def _mock_session_factory() -> MagicMock:
    """Create a mock async_sessionmaker that yields an AsyncMock session."""
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    ctx_manager = AsyncMock()
    ctx_manager.__aenter__ = AsyncMock(return_value=session)
    ctx_manager.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=ctx_manager)
    factory._mock_session = session  # expose for assertions
    return factory


def _make_callback(
    factory: MagicMock | None = None,
) -> tuple[IngestionCallback, MagicMock]:
    """Create an IngestionCallback with a mock session factory."""
    factory = factory or _mock_session_factory()
    callback = IngestionCallback(factory)
    return callback, factory


def _setup_job_mock(job_cls: MagicMock) -> MagicMock:
    """Configure a mocked JobRepository with all async methods."""
    repo = job_cls.return_value
    repo.update_status = AsyncMock()
    repo.propagate_failure = AsyncMock(return_value=[])
    return repo


class TestOnSuccess:
    """IngestionCallback.on_success — happy path."""

    async def test_material_processing_completed(self) -> None:
        """AuthoredDocument complete_processing called with the entry id only.

        Hotfix-9 dropped ``processed_content`` + ``processed_hash`` kwargs —
        per vision §1.2 those columns no longer live on AuthoredDocument.
        Method is now a pure state transition (pending → ready).
        """
        callback, _ = _make_callback()
        jid = uuid.uuid4()
        mid = uuid.uuid4()
        content = '{"key": "value"}'

        with (
            patch(_ENTRY_REPO) as entry_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            entry_cls.return_value.complete_processing = AsyncMock()
            job_cls.return_value.update_status = AsyncMock()

            await callback.on_success(job_id=jid, material_id=mid, content_json=content)

        entry_cls.return_value.complete_processing.assert_awaited_once()
        call_args = entry_cls.return_value.complete_processing.call_args
        assert call_args.args[0] == mid
        assert call_args.kwargs == {}

    async def test_job_updated_to_complete(self) -> None:
        """Job status transitions to 'complete'."""
        callback, _ = _make_callback()
        jid = uuid.uuid4()
        mid = uuid.uuid4()

        with (
            patch(_ENTRY_REPO) as entry_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            entry_cls.return_value.complete_processing = AsyncMock()
            job_repo = job_cls.return_value
            job_repo.update_status = AsyncMock()

            await callback.on_success(job_id=jid, material_id=mid, content_json="{}")

        job_repo.update_status.assert_awaited_once_with(jid, "complete")

    async def test_session_committed(self) -> None:
        """Session is committed after all updates."""
        callback, factory = _make_callback()
        session = factory._mock_session

        with (
            patch(_ENTRY_REPO) as entry_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            entry_cls.return_value.complete_processing = AsyncMock()
            job_cls.return_value.update_status = AsyncMock()

            await callback.on_success(
                job_id=uuid.uuid4(),
                material_id=uuid.uuid4(),
                content_json="{}",
            )

        session.commit.assert_awaited_once()

    async def test_fingerprint_hook_called(self) -> None:
        """_invalidate_fingerprints is called on success."""
        callback, _ = _make_callback()
        mid = uuid.uuid4()

        with (
            patch(_ENTRY_REPO) as entry_cls,
            patch(_JOB_REPO) as job_cls,
            patch.object(
                callback, "_invalidate_fingerprints", new_callable=AsyncMock
            ) as mock_fp,
        ):
            entry_cls.return_value.complete_processing = AsyncMock()
            job_cls.return_value.update_status = AsyncMock()

            await callback.on_success(
                job_id=uuid.uuid4(),
                material_id=mid,
                content_json="{}",
            )

        mock_fp.assert_awaited_once()
        call_kwargs = mock_fp.call_args
        assert call_kwargs.kwargs["material_id"] == mid

    async def test_repos_receive_same_session(self) -> None:
        """Both repositories are instantiated with the same session."""
        callback, factory = _make_callback()
        session = factory._mock_session

        with (
            patch(_ENTRY_REPO) as entry_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            entry_cls.return_value.complete_processing = AsyncMock()
            job_cls.return_value.update_status = AsyncMock()

            await callback.on_success(
                job_id=uuid.uuid4(),
                material_id=uuid.uuid4(),
                content_json="{}",
            )

        entry_cls.assert_called_once_with(session)
        job_cls.assert_called_once_with(session)


class TestOnFailure:
    """IngestionCallback.on_failure — error path."""

    async def test_job_updated_to_failed(self) -> None:
        """Job status transitions to 'failed' with error message."""
        callback, _ = _make_callback()
        jid = uuid.uuid4()
        mid = uuid.uuid4()
        error = "PDF parsing failed"

        with (
            patch(_ENTRY_REPO) as entry_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            entry_cls.return_value.fail_processing = AsyncMock()
            job_repo = _setup_job_mock(job_cls)

            await callback.on_failure(job_id=jid, material_id=mid, error_message=error)

        job_repo.update_status.assert_awaited_once_with(
            jid, "failed", error_message=error
        )

    async def test_material_updated_to_error(self) -> None:
        """AuthoredDocument fail_processing called with error message."""
        callback, _ = _make_callback()
        jid = uuid.uuid4()
        mid = uuid.uuid4()
        error = "Whisper timeout"

        with (
            patch(_ENTRY_REPO) as entry_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            entry_cls.return_value.fail_processing = AsyncMock()
            _setup_job_mock(job_cls)

            await callback.on_failure(job_id=jid, material_id=mid, error_message=error)

        entry_cls.return_value.fail_processing.assert_awaited_once_with(
            mid, error_message=error
        )

    async def test_session_committed(self) -> None:
        """Error session is committed after updates."""
        callback, factory = _make_callback()
        session = factory._mock_session

        with (
            patch(_ENTRY_REPO) as entry_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            entry_cls.return_value.fail_processing = AsyncMock()
            _setup_job_mock(job_cls)

            await callback.on_failure(
                job_id=uuid.uuid4(),
                material_id=uuid.uuid4(),
                error_message="some error",
            )

        session.commit.assert_awaited_once()

    async def test_repos_receive_same_session(self) -> None:
        """Both repositories are instantiated with the same session."""
        callback, factory = _make_callback()
        session = factory._mock_session

        with (
            patch(_ENTRY_REPO) as entry_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            entry_cls.return_value.fail_processing = AsyncMock()
            _setup_job_mock(job_cls)

            await callback.on_failure(
                job_id=uuid.uuid4(),
                material_id=uuid.uuid4(),
                error_message="error",
            )

        entry_cls.assert_called_once_with(session)
        job_cls.assert_called_once_with(session)


class TestOnSuccessErrors:
    """IngestionCallback.on_success — error propagation."""

    async def test_material_not_found_propagates(self) -> None:
        """ValueError from entry repo propagates to caller."""
        callback, _ = _make_callback()

        with (
            patch(_ENTRY_REPO) as entry_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            entry_cls.return_value.complete_processing = AsyncMock(
                side_effect=ValueError("AuthoredDocument not found: xxx")
            )
            job_cls.return_value.update_status = AsyncMock()

            with pytest.raises(ValueError, match="AuthoredDocument not found"):
                await callback.on_success(
                    job_id=uuid.uuid4(),
                    material_id=uuid.uuid4(),
                    content_json="{}",
                )

    async def test_job_not_found_propagates(self) -> None:
        """ValueError from job repo propagates to caller."""
        callback, _ = _make_callback()

        with (
            patch(_ENTRY_REPO) as entry_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            entry_cls.return_value.complete_processing = AsyncMock()
            job_cls.return_value.update_status = AsyncMock(
                side_effect=ValueError("Job xxx not found")
            )

            with pytest.raises(ValueError, match="Job xxx not found"):
                await callback.on_success(
                    job_id=uuid.uuid4(),
                    material_id=uuid.uuid4(),
                    content_json="{}",
                )


class TestOnFailureErrors:
    """IngestionCallback.on_failure — error propagation."""

    async def test_job_not_found_propagates(self) -> None:
        """ValueError from job repo propagates to caller."""
        callback, _ = _make_callback()

        with (
            patch(_ENTRY_REPO) as entry_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            entry_cls.return_value.fail_processing = AsyncMock()
            repo = _setup_job_mock(job_cls)
            repo.update_status = AsyncMock(side_effect=ValueError("Job not found"))

            with pytest.raises(ValueError, match="Job not found"):
                await callback.on_failure(
                    job_id=uuid.uuid4(),
                    material_id=uuid.uuid4(),
                    error_message="error",
                )


class TestHooksAreNoOp:
    """Extension hooks are callable and do nothing (yet)."""

    async def test_invalidate_fingerprints_is_noop(self) -> None:
        """_invalidate_fingerprints completes without error."""
        callback, _ = _make_callback()
        session = AsyncMock()
        await callback._invalidate_fingerprints(session, material_id=uuid.uuid4())


class TestCallbackIntegrationWithArqTask:
    """Verify arq_ingest_material delegates to IngestionCallback."""

    async def test_success_delegates_to_callback(self) -> None:
        """On successful processing, callback.on_success is called."""
        from course_supporter.api.tasks import arq_ingest_material

        jid = uuid.uuid4()
        mid = uuid.uuid4()
        mock_doc = MagicMock()
        mock_doc.model_dump_json.return_value = '{"content": "ok"}'
        # metadata must be a real dict so .get() returns None (no
        # detected_language to cache).
        mock_doc.metadata = {}

        mock_processor = MagicMock()
        mock_processor.process_raw = AsyncMock(return_value=mock_doc)

        session = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()

        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=session)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)

        factory = MagicMock(return_value=ctx_manager)
        router = MagicMock()

        mock_entry = MagicMock()
        mock_entry.source_url = "https://example.com"

        ctx = {"session_factory": factory, "model_router": router}

        _arq_job_repo = "course_supporter.storage.job_repository.JobRepository"
        _arq_entry_repo = (
            "course_supporter.storage.authored_document_repository"
            ".AuthoredDocumentRepository"
        )
        _factory = "course_supporter.api.tasks.create_processors"
        _heavy = "course_supporter.api.tasks.create_heavy_steps"

        with (
            patch("course_supporter.ingestion_callback.IngestionCallback") as cb_cls,
            patch("course_supporter.job_priority.check_work_window"),
            patch(_arq_job_repo) as job_cls,
            patch(_arq_entry_repo) as entry_cls,
            patch(_heavy),
            patch(_factory, return_value={"web": mock_processor}),
            patch(
                "course_supporter.api.tasks.set_tenant_from_job",
                new=AsyncMock(),
            ),
        ):
            job_cls.return_value.update_status = AsyncMock()
            entry_cls.return_value.get_by_id = AsyncMock(return_value=mock_entry)
            entry_cls.return_value.set_pending = AsyncMock()
            cb_cls.return_value.on_success = AsyncMock()
            cb_cls.return_value.on_failure = AsyncMock()

            await arq_ingest_material(
                ctx, str(jid), str(mid), "web", "https://example.com"
            )

        cb_cls.return_value.on_success.assert_awaited_once()
        call_kwargs = cb_cls.return_value.on_success.call_args.kwargs
        assert call_kwargs["job_id"] == jid
        assert call_kwargs["material_id"] == mid
        assert call_kwargs["content_json"] == '{"content": "ok"}'

    async def test_failure_delegates_to_callback(self) -> None:
        """On processing error, callback.on_failure is called."""
        from course_supporter.api.tasks import arq_ingest_material

        jid = uuid.uuid4()
        mid = uuid.uuid4()

        session = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()

        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=session)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)

        factory = MagicMock(return_value=ctx_manager)
        router = MagicMock()

        ctx = {"session_factory": factory, "model_router": router}

        _arq_job_repo = "course_supporter.storage.job_repository.JobRepository"
        _arq_entry_repo = (
            "course_supporter.storage.authored_document_repository"
            ".AuthoredDocumentRepository"
        )
        _heavy = "course_supporter.api.tasks.create_heavy_steps"
        _factory_fn = "course_supporter.api.tasks.create_processors"

        mock_processor = MagicMock()
        mock_processor.process_raw = AsyncMock(side_effect=RuntimeError("boom"))

        mock_entry = MagicMock()
        mock_entry.source_url = "https://example.com"

        with (
            patch("course_supporter.ingestion_callback.IngestionCallback") as cb_cls,
            patch("course_supporter.job_priority.check_work_window"),
            patch(_arq_job_repo) as job_cls,
            patch(_arq_entry_repo) as entry_cls,
            patch(_heavy),
            patch(_factory_fn, return_value={"web": mock_processor}),
            patch(
                "course_supporter.api.tasks.set_tenant_from_job",
                new=AsyncMock(),
            ),
        ):
            job_cls.return_value.update_status = AsyncMock()
            entry_cls.return_value.get_by_id = AsyncMock(return_value=mock_entry)
            entry_cls.return_value.set_pending = AsyncMock()
            cb_cls.return_value.on_success = AsyncMock()
            cb_cls.return_value.on_failure = AsyncMock()

            await arq_ingest_material(
                ctx, str(jid), str(mid), "web", "https://example.com"
            )

        cb_cls.return_value.on_failure.assert_awaited_once()
        call_kwargs = cb_cls.return_value.on_failure.call_args.kwargs
        assert call_kwargs["job_id"] == jid
        assert call_kwargs["material_id"] == mid
        assert "boom" in call_kwargs["error_message"]
