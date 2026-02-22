"""Tests for IngestionCallback service."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.ingestion_callback import IngestionCallback

# Top-level imports in ingestion_callback — patch on the importing module
_MAT_REPO = "course_supporter.ingestion_callback.SourceMaterialRepository"
_JOB_REPO = "course_supporter.ingestion_callback.JobRepository"
_VALIDATION_SVC = "course_supporter.storage.mapping_validation.MappingValidationService"


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


class TestOnSuccess:
    """IngestionCallback.on_success — happy path."""

    @pytest.fixture(autouse=True)
    def _mock_revalidation(self) -> None:  # type: ignore[misc]
        with patch(_VALIDATION_SVC) as svc:
            svc.return_value.revalidate_blocked = AsyncMock(return_value=0)
            yield

    async def test_material_updated_to_done(self) -> None:
        """SourceMaterial status transitions to 'done' with content."""
        callback, _ = _make_callback()
        jid = uuid.uuid4()
        mid = uuid.uuid4()
        content = '{"key": "value"}'

        with (
            patch(_MAT_REPO) as mat_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            mat_repo = mat_cls.return_value
            mat_repo.update_status = AsyncMock()
            job_cls.return_value.update_status = AsyncMock()

            await callback.on_success(job_id=jid, material_id=mid, content_json=content)

        mat_repo.update_status.assert_awaited_once_with(
            mid, "done", content_snapshot=content
        )

    async def test_job_updated_to_complete(self) -> None:
        """Job status transitions to 'complete' with result_material_id."""
        callback, _ = _make_callback()
        jid = uuid.uuid4()
        mid = uuid.uuid4()

        with (
            patch(_MAT_REPO) as mat_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            mat_cls.return_value.update_status = AsyncMock()
            job_repo = job_cls.return_value
            job_repo.update_status = AsyncMock()

            await callback.on_success(job_id=jid, material_id=mid, content_json="{}")

        job_repo.update_status.assert_awaited_once_with(
            jid, "complete", result_material_id=mid
        )

    async def test_session_committed(self) -> None:
        """Session is committed after all updates."""
        callback, factory = _make_callback()
        session = factory._mock_session

        with (
            patch(_MAT_REPO) as mat_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            mat_cls.return_value.update_status = AsyncMock()
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
            patch(_MAT_REPO) as mat_cls,
            patch(_JOB_REPO) as job_cls,
            patch.object(
                callback, "_invalidate_fingerprints", new_callable=AsyncMock
            ) as mock_fp,
        ):
            mat_cls.return_value.update_status = AsyncMock()
            job_cls.return_value.update_status = AsyncMock()

            await callback.on_success(
                job_id=uuid.uuid4(),
                material_id=mid,
                content_json="{}",
            )

        mock_fp.assert_awaited_once()
        call_kwargs = mock_fp.call_args
        assert call_kwargs.kwargs["material_id"] == mid

    async def test_revalidate_hook_called(self) -> None:
        """_revalidate_blocked_mappings is called on success."""
        callback, _ = _make_callback()
        mid = uuid.uuid4()

        with (
            patch(_MAT_REPO) as mat_cls,
            patch(_JOB_REPO) as job_cls,
            patch.object(
                callback,
                "_revalidate_blocked_mappings",
                new_callable=AsyncMock,
            ) as mock_rv,
        ):
            mat_cls.return_value.update_status = AsyncMock()
            job_cls.return_value.update_status = AsyncMock()

            await callback.on_success(
                job_id=uuid.uuid4(),
                material_id=mid,
                content_json="{}",
            )

        mock_rv.assert_awaited_once()
        call_kwargs = mock_rv.call_args
        assert call_kwargs.kwargs["material_id"] == mid

    async def test_repos_receive_same_session(self) -> None:
        """Both repositories are instantiated with the same session."""
        callback, factory = _make_callback()
        session = factory._mock_session

        with (
            patch(_MAT_REPO) as mat_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            mat_cls.return_value.update_status = AsyncMock()
            job_cls.return_value.update_status = AsyncMock()

            await callback.on_success(
                job_id=uuid.uuid4(),
                material_id=uuid.uuid4(),
                content_json="{}",
            )

        mat_cls.assert_called_once_with(session)
        job_cls.assert_called_once_with(session)


class TestOnFailure:
    """IngestionCallback.on_failure — error path."""

    @pytest.fixture(autouse=True)
    def _mock_revalidation(self) -> None:  # type: ignore[misc]
        with patch(_VALIDATION_SVC) as svc:
            svc.return_value.revalidate_blocked = AsyncMock(return_value=0)
            yield

    async def test_job_updated_to_failed(self) -> None:
        """Job status transitions to 'failed' with error message."""
        callback, _ = _make_callback()
        jid = uuid.uuid4()
        mid = uuid.uuid4()
        error = "PDF parsing failed"

        with (
            patch(_MAT_REPO) as mat_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            mat_cls.return_value.update_status = AsyncMock()
            job_repo = job_cls.return_value
            job_repo.update_status = AsyncMock()

            await callback.on_failure(job_id=jid, material_id=mid, error_message=error)

        job_repo.update_status.assert_awaited_once_with(
            jid, "failed", error_message=error
        )

    async def test_material_updated_to_error(self) -> None:
        """SourceMaterial status transitions to 'error' with message."""
        callback, _ = _make_callback()
        jid = uuid.uuid4()
        mid = uuid.uuid4()
        error = "Whisper timeout"

        with (
            patch(_MAT_REPO) as mat_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            mat_repo = mat_cls.return_value
            mat_repo.update_status = AsyncMock()
            job_cls.return_value.update_status = AsyncMock()

            await callback.on_failure(job_id=jid, material_id=mid, error_message=error)

        mat_repo.update_status.assert_awaited_once_with(
            mid, "error", error_message=error
        )

    async def test_session_committed(self) -> None:
        """Error session is committed after updates."""
        callback, factory = _make_callback()
        session = factory._mock_session

        with (
            patch(_MAT_REPO) as mat_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            mat_cls.return_value.update_status = AsyncMock()
            job_cls.return_value.update_status = AsyncMock()

            await callback.on_failure(
                job_id=uuid.uuid4(),
                material_id=uuid.uuid4(),
                error_message="some error",
            )

        session.commit.assert_awaited_once()

    async def test_revalidate_hook_called_on_failure(self) -> None:
        """_revalidate_blocked_mappings called on failure too.

        When material fails, blocked mappings need their
        blocking_factors updated (material_error type).
        """
        callback, _ = _make_callback()
        mid = uuid.uuid4()

        with (
            patch(_MAT_REPO) as mat_cls,
            patch(_JOB_REPO) as job_cls,
            patch.object(
                callback,
                "_revalidate_blocked_mappings",
                new_callable=AsyncMock,
            ) as mock_rv,
        ):
            mat_cls.return_value.update_status = AsyncMock()
            job_cls.return_value.update_status = AsyncMock()

            await callback.on_failure(
                job_id=uuid.uuid4(),
                material_id=mid,
                error_message="error",
            )

        mock_rv.assert_awaited_once()
        call_kwargs = mock_rv.call_args
        assert call_kwargs.kwargs["material_id"] == mid

    async def test_repos_receive_same_session(self) -> None:
        """Both repositories are instantiated with the same session."""
        callback, factory = _make_callback()
        session = factory._mock_session

        with (
            patch(_MAT_REPO) as mat_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            mat_cls.return_value.update_status = AsyncMock()
            job_cls.return_value.update_status = AsyncMock()

            await callback.on_failure(
                job_id=uuid.uuid4(),
                material_id=uuid.uuid4(),
                error_message="error",
            )

        mat_cls.assert_called_once_with(session)
        job_cls.assert_called_once_with(session)


class TestOnSuccessErrors:
    """IngestionCallback.on_success — error propagation."""

    @pytest.fixture(autouse=True)
    def _mock_revalidation(self) -> None:  # type: ignore[misc]
        with patch(_VALIDATION_SVC) as svc:
            svc.return_value.revalidate_blocked = AsyncMock(return_value=0)
            yield

    async def test_material_not_found_propagates(self) -> None:
        """ValueError from material repo propagates to caller."""
        callback, _ = _make_callback()

        with (
            patch(_MAT_REPO) as mat_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            mat_cls.return_value.update_status = AsyncMock(
                side_effect=ValueError("SourceMaterial not found: xxx")
            )
            job_cls.return_value.update_status = AsyncMock()

            with pytest.raises(ValueError, match="SourceMaterial not found"):
                await callback.on_success(
                    job_id=uuid.uuid4(),
                    material_id=uuid.uuid4(),
                    content_json="{}",
                )

    async def test_job_not_found_propagates(self) -> None:
        """ValueError from job repo propagates to caller."""
        callback, _ = _make_callback()

        with (
            patch(_MAT_REPO) as mat_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            mat_cls.return_value.update_status = AsyncMock()
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

    @pytest.fixture(autouse=True)
    def _mock_revalidation(self) -> None:  # type: ignore[misc]
        with patch(_VALIDATION_SVC) as svc:
            svc.return_value.revalidate_blocked = AsyncMock(return_value=0)
            yield

    async def test_job_not_found_propagates(self) -> None:
        """ValueError from job repo propagates to caller."""
        callback, _ = _make_callback()

        with (
            patch(_MAT_REPO) as mat_cls,
            patch(_JOB_REPO) as job_cls,
        ):
            mat_cls.return_value.update_status = AsyncMock()
            job_cls.return_value.update_status = AsyncMock(
                side_effect=ValueError("Job not found")
            )

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


class TestRevalidateBlockedMappingsCallback:
    """_revalidate_blocked_mappings delegates to MappingValidationService."""

    async def test_success_triggers_revalidation(self) -> None:
        """on_success() calls MappingValidationService.revalidate_blocked."""
        callback, _ = _make_callback()
        mid = uuid.uuid4()

        with (
            patch(_MAT_REPO) as mat_cls,
            patch(_JOB_REPO) as job_cls,
            patch(_VALIDATION_SVC) as svc_cls,
        ):
            mat_cls.return_value.update_status = AsyncMock()
            job_cls.return_value.update_status = AsyncMock()
            svc_cls.return_value.revalidate_blocked = AsyncMock(return_value=0)

            await callback.on_success(
                job_id=uuid.uuid4(), material_id=mid, content_json="{}"
            )

        svc_cls.return_value.revalidate_blocked.assert_awaited_once_with(mid)

    async def test_failure_triggers_revalidation(self) -> None:
        """on_failure() also calls MappingValidationService.revalidate_blocked."""
        callback, _ = _make_callback()
        mid = uuid.uuid4()

        with (
            patch(_MAT_REPO) as mat_cls,
            patch(_JOB_REPO) as job_cls,
            patch(_VALIDATION_SVC) as svc_cls,
        ):
            mat_cls.return_value.update_status = AsyncMock()
            job_cls.return_value.update_status = AsyncMock()
            svc_cls.return_value.revalidate_blocked = AsyncMock(return_value=0)

            await callback.on_failure(
                job_id=uuid.uuid4(), material_id=mid, error_message="error"
            )

        svc_cls.return_value.revalidate_blocked.assert_awaited_once_with(mid)


class TestCallbackIntegrationWithArqTask:
    """Verify arq_ingest_material delegates to IngestionCallback."""

    async def test_success_delegates_to_callback(self) -> None:
        """On successful processing, callback.on_success is called."""
        from course_supporter.api.tasks import arq_ingest_material

        jid = uuid.uuid4()
        mid = uuid.uuid4()
        mock_doc = MagicMock()
        mock_doc.model_dump_json.return_value = '{"content": "ok"}'

        mock_processor = MagicMock()
        mock_processor.process = AsyncMock(return_value=mock_doc)

        session = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()

        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=session)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)

        factory = MagicMock(return_value=ctx_manager)
        router = MagicMock()

        mock_material = MagicMock()

        ctx = {"session_factory": factory, "model_router": router}

        _arq_job_repo = "course_supporter.storage.job_repository.JobRepository"
        _factory = "course_supporter.api.tasks.create_processors"
        _heavy = "course_supporter.api.tasks.create_heavy_steps"

        with (
            patch("course_supporter.ingestion_callback.IngestionCallback") as cb_cls,
            patch("course_supporter.job_priority.check_work_window"),
            patch(_arq_job_repo) as job_cls,
            patch("course_supporter.api.tasks.SourceMaterialRepository") as mat_cls,
            patch(_heavy),
            patch(_factory, return_value={"web": mock_processor}),
        ):
            job_cls.return_value.update_status = AsyncMock()
            mat_cls.return_value.update_status = AsyncMock()
            mat_cls.return_value.get_by_id = AsyncMock(return_value=mock_material)
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
        _heavy = "course_supporter.api.tasks.create_heavy_steps"
        _factory_fn = "course_supporter.api.tasks.create_processors"

        with (
            patch("course_supporter.ingestion_callback.IngestionCallback") as cb_cls,
            patch("course_supporter.job_priority.check_work_window"),
            patch(_arq_job_repo) as job_cls,
            patch("course_supporter.api.tasks.SourceMaterialRepository") as mat_cls,
            patch(_heavy),
            patch(_factory_fn, return_value={"web": MagicMock()}),
        ):
            job_cls.return_value.update_status = AsyncMock()
            mat_cls.return_value.update_status = AsyncMock()
            mat_cls.return_value.get_by_id = AsyncMock(return_value=None)
            cb_cls.return_value.on_success = AsyncMock()
            cb_cls.return_value.on_failure = AsyncMock()

            await arq_ingest_material(
                ctx, str(jid), str(mid), "web", "https://example.com"
            )

        cb_cls.return_value.on_failure.assert_awaited_once()
        call_kwargs = cb_cls.return_value.on_failure.call_args.kwargs
        assert call_kwargs["job_id"] == jid
        assert call_kwargs["material_id"] == mid
        assert "not found" in call_kwargs["error_message"]
