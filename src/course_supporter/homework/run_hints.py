"""Run hints: what a student's project carries for the code-run service.

A pure function over the paths of a project snapshot: which files are tests,
which declare dependencies, which look like an entry point. No I/O, no models,
no network. The code-run port (a later change) reads these hints to propose a
run profile's defaults and to tell the author's tests from the student's.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

# A Python file inside a directory with one of these names is a test file.
_TEST_DIR_NAMES: Final[frozenset[str]] = frozenset({"tests", "test"})

# Files that declare what a project installs (compared lower-cased).
# requirements*.txt is matched by prefix and suffix, not listed here.
_DEPENDENCY_FILE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "pipfile",
        "pipfile.lock",
        "poetry.lock",
        "uv.lock",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
    }
)

# Conventional names of a Python entry point (compared lower-cased).
_ENTRY_FILE_NAMES: Final[frozenset[str]] = frozenset(
    {"main.py", "__main__.py", "app.py", "manage.py"}
)


@dataclass(frozen=True, slots=True)
class RunHints:
    """What the paths of a snapshot say about running it.

    Every tuple is sorted and holds each path once, written with forward
    slashes and without a leading ``./``.
    """

    test_files: tuple[str, ...]
    dependency_files: tuple[str, ...]
    entry_candidates: tuple[str, ...]

    @property
    def has_tests(self) -> bool:
        """Whether the snapshot carries at least one test file."""
        return bool(self.test_files)


def _normalized(raw: str) -> PurePosixPath:
    return PurePosixPath(raw.replace("\\", "/"))


def _is_test_file(path: PurePosixPath) -> bool:
    if path.suffix.lower() != ".py":
        return False
    name = path.name.lower()
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return any(part.lower() in _TEST_DIR_NAMES for part in path.parts[:-1])


def _is_dependency_file(path: PurePosixPath) -> bool:
    name = path.name.lower()
    if name in _DEPENDENCY_FILE_NAMES:
        return True
    return name.startswith("requirements") and name.endswith(".txt")


def _is_entry_candidate(path: PurePosixPath) -> bool:
    return path.name.lower() in _ENTRY_FILE_NAMES and not _is_test_file(path)


def detect_run_hints(paths: Iterable[str]) -> RunHints:
    """Classify the paths of a project snapshot for the code-run service."""
    tests: set[str] = set()
    dependencies: set[str] = set()
    entries: set[str] = set()
    for raw in paths:
        path = _normalized(raw)
        text = str(path)
        if _is_test_file(path):
            tests.add(text)
        if _is_dependency_file(path):
            dependencies.add(text)
        if _is_entry_candidate(path):
            entries.add(text)
    return RunHints(
        test_files=tuple(sorted(tests)),
        dependency_files=tuple(sorted(dependencies)),
        entry_candidates=tuple(sorted(entries)),
    )
