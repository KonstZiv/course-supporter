"""The six boot checks live in one place, and no process runs its own.

``boot.build_checked_stage_router`` loads the registry and the ladders, runs the
checks and builds the StageRouter for every process -- the API's
``api/app.py::lifespan`` and the workers' ``worker.py::startup`` (Б1). Before
it, the two boot places were mirrored by hand, and a check added to one and
forgotten in the other was invisible until the half that skipped it served a
review.

The structural checks here pin the shape: every expected check is called in the
shared function, and neither boot place calls a check (or loads the ladders)
itself. They generalise: they notice the NEXT validator written straight into
a boot place, which no behavioural test written today can. The behavioural
locks are ``test_one_startup.py`` (each check, failing, stops every process)
and ``TestLifespanActuallyRuns`` below (real broken config, real lifespan).

What a structural check cannot see, measured rather than assumed: a call in
dead code. Rewriting ``validate_phrasebook(...)`` as
``if False: validate_phrasebook(...)`` leaves the call in the tree and the
check unexecuted, and this passes. That is why it is not the only lock.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastapi import FastAPI

from course_supporter import boot, language
from course_supporter.config import get_settings

# ``import course_supporter.api.app`` binds the package's ``app`` attribute --
# the FastAPI instance -- not the module. import_module returns the module.
app_module = importlib.import_module("course_supporter.api.app")
worker_module = importlib.import_module("course_supporter.worker")

# Every validator the boot is expected to run. The three at the end are task
# 04's; the rest were already there and are pinned so this test notices a check
# being dropped as readily as one being added to a single half.
EXPECTED_CHECKS = frozenset(
    {
        "validate_ladders_against_registry",
        "validate_path_config",
        "validate_stage_executors",
        "validate_ladder_prompts",
        "validate_phrasebook",
        "validate_native_names",
    }
)


def _function(path: Path, function: str) -> ast.AST:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            and node.name == function
        ):
            return node
    pytest.fail(f"{path.name} has no function named {function!r}")


def _called_names(path: Path, function: str) -> frozenset[str]:
    """The names called inside one function of a module.

    Parsed, not grepped: a validator's name appears in the comments beside the
    calls too, and a comment is not a call. Both ``name(...)`` and
    ``module.name(...)`` count.
    """
    names: set[str] = set()
    for child in ast.walk(_function(path, function)):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Name):
            names.add(child.func.id)
        elif isinstance(child.func, ast.Attribute):
            names.add(child.func.attr)
    return frozenset(names)


SHARED = (Path(boot.__file__), "build_checked_stage_router")
BOOT_PLACES = [
    (Path(worker_module.__file__), "startup"),
    (Path(app_module.__file__), "lifespan"),
]

# What only the shared function may do: load what the checks read, and build
# the router from it.
SHARED_ONLY = EXPECTED_CHECKS | {
    "load_registry",
    "load_ladder_config",
    "get_path_config",
    "create_stage_router",
}


class TestTheChecksLiveInTheSharedStartup:
    def test_the_shared_function_runs_every_expected_check(self) -> None:
        missing = EXPECTED_CHECKS - _called_names(*SHARED)

        assert not missing, (
            f"boot.build_checked_stage_router does not run: {sorted(missing)}"
        )

    @pytest.mark.parametrize(
        ("path", "function"), BOOT_PLACES, ids=["worker", "lifespan"]
    )
    def test_no_boot_place_runs_a_check_itself(self, path: Path, function: str) -> None:
        called = _called_names(path, function)
        own = {name for name in called if name.startswith("validate")}
        own |= called & SHARED_ONLY

        assert not own, (
            f"{path.name}::{function} runs {sorted(own)} itself; it belongs in "
            "boot.build_checked_stage_router, which every process calls."
        )

    @pytest.mark.parametrize(
        ("path", "function"), BOOT_PLACES, ids=["worker", "lifespan"]
    )
    def test_every_boot_place_calls_the_shared_function(
        self, path: Path, function: str
    ) -> None:
        assert "build_checked_stage_router" in _called_names(path, function)


class TestLifespanActuallyRuns:
    """The lifespan raises on broken config — before it opens anything.

    The three checks all sit ahead of the ARQ pool and the S3 bucket, so a
    broken configuration stops the boot before any I/O and this needs no
    Redis and no bucket. ``create_pool`` is patched only to prove it was never
    reached: if a check were moved after it, or wrapped in dead code, that
    assertion is what would notice.
    """

    @staticmethod
    def _boot_fails(monkeypatch: pytest.MonkeyPatch, match: str) -> None:
        """Run the lifespan and require it to refuse before opening anything."""
        pool = AsyncMock()
        monkeypatch.setattr(app_module, "create_pool", pool)
        monkeypatch.setattr(app_module, "configure_logging", MagicMock())

        async def _run() -> None:
            async with app_module.lifespan(FastAPI()):
                pass  # pragma: no cover — the boot must not get this far

        with pytest.raises(ValueError, match=match):
            asyncio.run(_run())
        assert pool.await_count == 0, "a check ran after the pool was opened"

    def test_a_phrasebook_missing_a_key_stops_the_boot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        book = tmp_path / "phrasebook"
        book.mkdir()
        for path in sorted(get_settings().phrasebook_dir.glob("*.yaml")):
            (book / path.name).write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8"
            )
        english = book / "eng.yaml"
        english.write_text(
            "".join(
                line
                for line in english.read_text(encoding="utf-8").splitlines(
                    keepends=True
                )
                if not line.startswith("remark.why:")
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(get_settings(), "phrasebook_dir", book)

        self._boot_fails(monkeypatch, re.escape("remark.why"))

    def test_a_language_with_no_native_name_stops_the_boot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Captured BEFORE the patch: inside the test body monkeypatch has not
        # undone it yet, so reading the setting here would hand back the broken
        # file and re-cache it for everyone after — which is exactly what the
        # first version of this test did.
        real = get_settings().language_names_path
        raw = yaml.safe_load(real.read_text(encoding="utf-8"))
        del raw["heb"]
        broken = tmp_path / "language_names.yaml"
        broken.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(get_settings(), "language_names_path", broken)

        try:
            self._boot_fails(monkeypatch, "heb")
        finally:
            # The loader caches and an explicit path replaces the cache, so the
            # broken read must not outlive this test.
            language.load_native_names(real)

    def test_an_unreadable_ladder_prompt_stops_the_boot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ladders = tmp_path / "ladders"
        ladders.mkdir()
        for path in sorted(get_settings().ladders_dir.glob("ladders_*.yaml")):
            (ladders / path.name).write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8"
            )
        target = ladders / "ladders_mentor.yaml"
        target.write_text(
            target.read_text(encoding="utf-8").replace(
                "prompts/mentor_synthesis/v1.md", "prompts/mentor_synthesis/v9.md"
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(get_settings(), "ladders_dir", ladders)

        self._boot_fails(monkeypatch, re.escape("v9.md"))
