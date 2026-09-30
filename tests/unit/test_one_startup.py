"""One startup for every process (Б1).

The API (``api/app.py::lifespan``) and both ARQ workers (``WorkerSettings``,
``HomeworkWorkerSettings`` -- the three processes of prod) load the model
registry and the stage ladders, run the six boot checks and build the
StageRouter through one function,
:func:`course_supporter.boot.build_checked_stage_router`.
These locks hold that from three sides:

* the processes really call the shared function -- replace it with one that
  fails and none of the three can start;
* every one of the six checks, failing, stops every process;
* the startup log events keep their names and their order -- the post-deploy
  measurement reads them as "every check passed".
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from fastapi import FastAPI
from pydantic import SecretStr
from structlog.testing import capture_logs

from course_supporter import boot
from course_supporter.config import Settings
from course_supporter.llm import factory
from course_supporter.storage import s3 as s3_module
from course_supporter.storage.s3 import S3Client
from course_supporter.worker import HomeworkWorkerSettings, WorkerSettings

# ``import course_supporter.api.app`` binds the package's ``app`` attribute --
# the FastAPI instance -- not the module. import_module returns the module.
app_module = importlib.import_module("course_supporter.api.app")

StartProcess = Callable[[], Awaitable[None]]


async def _start_api() -> None:
    async with app_module.lifespan(FastAPI()):
        pass


async def _start_worker() -> None:
    await WorkerSettings.on_startup({})


async def _start_homework_worker() -> None:
    await HomeworkWorkerSettings.on_startup({})


PROCESSES: list[StartProcess] = [_start_api, _start_worker, _start_homework_worker]
PROCESS_IDS = ["api", "worker", "homework_worker"]


class _FakeProvider:
    """Stands in for an LLM provider: built at startup, never called."""

    def __init__(self, **_: Any) -> None:
        pass


class _FakeKeyPool:
    def all_keys(self) -> list[SecretStr]:
        return [SecretStr("not-a-key")]


@pytest.fixture(autouse=True)
def _no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup without Redis, S3, real keys or a reconfigured logger.

    One provider is registered from a fake key pool, so the provider event is
    the same on every machine whatever keys its environment carries. S3 gets a
    client whose ``head_bucket`` answers, so ``ensure_bucket`` runs for real
    and writes its own event.

    The module loggers on the startup path are swapped for fresh ones: a
    logger another test already bound (``cache_logger_on_first_use``) keeps the
    processors of its day and slips past ``capture_logs``. Which events the
    code writes does not change -- only who hears them.
    """
    for module in (app_module, factory, s3_module):
        monkeypatch.setattr(module, "logger", structlog.get_logger())
    monkeypatch.setattr(app_module, "configure_logging", MagicMock())
    monkeypatch.setattr("course_supporter.worker.configure_logging", MagicMock())
    monkeypatch.setattr(app_module, "create_pool", AsyncMock())
    monkeypatch.setattr(factory, "PROVIDER_REGISTRY", {"gemini": _FakeProvider})
    monkeypatch.setattr(
        Settings,
        "key_pool_for",
        lambda _self, name: _FakeKeyPool() if name == "gemini" else None,
    )

    async def _open(self: S3Client) -> None:
        self._client = AsyncMock()

    async def _close(self: S3Client) -> None:
        self._client = None

    monkeypatch.setattr(S3Client, "open", _open)
    monkeypatch.setattr(S3Client, "close", _close)


# The six boot checks, by the name the shared function calls them.
BOOT_CHECKS = [
    "validate_ladders_against_registry",
    "validate_path_config",
    "validate_stage_executors",
    "validate_ladder_prompts",
    "validate_phrasebook",
    "validate_native_names",
]

# What a process writes once it is up; a refused boot must never get there.
STARTED_EVENTS = {"app_started", "worker_started"}


class TestEveryProcessCallsTheSharedStartup:
    """Replace the shared function with one that fails: no process starts.

    A process that loads and checks its configuration on its own, past the
    shared function, would start here -- and turn this red.
    """

    @pytest.mark.parametrize("start", PROCESSES, ids=PROCESS_IDS)
    async def test_a_failing_shared_startup_stops_the_process(
        self, monkeypatch: pytest.MonkeyPatch, start: StartProcess
    ) -> None:
        failing = MagicMock(side_effect=RuntimeError("shared startup refused"))
        monkeypatch.setattr(boot, "build_checked_stage_router", failing)

        with (
            capture_logs() as logs,
            pytest.raises(RuntimeError, match="shared startup refused"),
        ):
            await start()

        failing.assert_called_once()
        assert STARTED_EVENTS.isdisjoint(entry["event"] for entry in logs)


class TestEveryCheckStopsEveryProcess:
    """Each of the six checks, failing, stops the API and both workers."""

    @pytest.mark.parametrize("start", PROCESSES, ids=PROCESS_IDS)
    @pytest.mark.parametrize("check", BOOT_CHECKS)
    async def test_a_failing_check_stops_the_process(
        self, monkeypatch: pytest.MonkeyPatch, check: str, start: StartProcess
    ) -> None:
        refused = MagicMock(side_effect=ValueError(f"{check} refused"))
        monkeypatch.setattr(boot, check, refused)
        pool = AsyncMock()
        monkeypatch.setattr(app_module, "create_pool", pool)

        with capture_logs() as logs, pytest.raises(ValueError, match=check):
            await start()

        refused.assert_called_once()
        assert pool.await_count == 0, "the API opened its Redis pool first"
        assert STARTED_EVENTS.isdisjoint(entry["event"] for entry in logs)


class TestStartupLogEvents:
    """Names and order of the startup events, pinned from ``main`` before Б1."""

    async def test_api(self) -> None:
        with capture_logs() as logs:
            await _start_api()

        assert [entry["event"] for entry in logs] == [
            "llm_provider_registered",
            "s3_bucket_verified",
            "app_started",
            "app_stopped",
        ]

    @pytest.mark.parametrize(
        "start", [_start_worker, _start_homework_worker], ids=PROCESS_IDS[1:]
    )
    async def test_worker(self, start: StartProcess) -> None:
        with capture_logs() as logs:
            await start()

        assert [entry["event"] for entry in logs] == [
            "llm_provider_registered",
            "worker_started",
        ]
