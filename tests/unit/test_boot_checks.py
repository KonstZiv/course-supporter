"""The two boot places run the same checks (mentor-rebuild task 04).

``worker.py::startup`` and ``api/app.py::lifespan`` are mirrored by hand: the
same configuration must be refused in both, because either process alone can be
the one a deploy brings up first. Nothing enforces the mirroring — it is two
lists of calls in two files — so a check added to one and forgotten in the
other is invisible until the half that skipped it serves a review.

The worker's half is locked by the boot tests in ``test_worker.py``, which run
``startup`` against broken config. The lifespan cannot be run that way: it
opens an ARQ pool and an S3 bucket. So this reads both functions instead and
compares which validators each one calls. It is a structural lock, not a
behavioural one, and it is here because the alternative is no lock at all.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

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


def _called_names(path: Path, function: str) -> frozenset[str]:
    """The plain function names called inside one function of a module.

    Parsed, not grepped: a validator's name appears in the comments beside the
    calls too, and a comment is not a call.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            and node.name == function
        ):
            return frozenset(
                child.func.id
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            )
    pytest.fail(f"{path.name} has no function named {function!r}")


BOOT_PLACES = [
    (Path(worker_module.__file__), "startup"),
    (Path(app_module.__file__), "lifespan"),
]


class TestBothBootPlacesCheckTheSameThings:
    @pytest.mark.parametrize(
        ("path", "function"), BOOT_PLACES, ids=["worker", "lifespan"]
    )
    def test_every_expected_check_runs(self, path: Path, function: str) -> None:
        missing = EXPECTED_CHECKS - _called_names(path, function)

        assert not missing, (
            f"{path.name}::{function} does not run: {sorted(missing)}. "
            "A check belongs in both boot places or in neither."
        )

    def test_neither_half_has_a_check_the_other_lacks(self) -> None:
        """Catches the next one too, not only the six named above."""
        worker, lifespan = (_called_names(path, fn) for path, fn in BOOT_PLACES)
        validators = {name for name in worker | lifespan if name.startswith("validate")}

        assert validators - worker == set(), (
            "lifespan checks something the worker does not"
        )
        assert validators - lifespan == set(), (
            "the worker checks something lifespan does not"
        )
