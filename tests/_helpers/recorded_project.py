"""A recorded project, read through the real project door (project chain K4).

The locks of chain K4 need what a project's stages are really handed: the
delta context in its frames, the student's files and the author's base. This
helper runs the real door (``process_project_submission``, as the new path
asks it, ``base_corpus=True``) over a small recorded project, with the fakes
of ``tests/unit/test_project_submission.py`` standing in for S3 and the
repositories. No network, no database.

The project, every frame of the context in it:

* ``app.py`` — CHANGED: the author's loader, which the student extended. The
  import, the signature and the call the base had are the author's, left as
  they were; its first line names ``utils.py``.
* ``stats.py`` — NEW: the student's own module.
* ``utils.py`` — unchanged and named by a changed file: a NEIGHBOR, the
  author's code shown for reference.
* ``README.md`` — unchanged and named by no one: in the trusted tree only.
* ``.env`` — never opened: the block of what was not opened closes the text.
"""

from __future__ import annotations

import uuid

import pytest
import structlog

from course_supporter.homework import project_submission as door_module
from course_supporter.homework.doors import DoorReading
from course_supporter.homework.project_submission import process_project_submission
from course_supporter.normalizer import manifest_to_jsonb, normalize_archive
from tests.unit.test_project_submission import (
    _FakeBase,
    _FakeHwRepo,
    _FakeRepo,
    _FakeS3,
    _FakeSession,
    _FakeSubmission,
    _zip_bytes,
)

AUTHORS_APP = (
    "from utils import read_lines\n"
    "\n"
    "\n"
    "def main(path):\n"
    "    lines = read_lines(path)\n"
    "    print(len(lines))\n"
)
UTILS = (
    "def read_lines(path):\n"
    '    with open(path, encoding="utf-8") as handle:\n'
    "        return handle.read().splitlines()\n"
)
README = "# Line statistics\n\nRun `python app.py data.txt`.\n"

STUDENTS_APP = (
    "# Reads its files through utils.py, as the starting project does.\n"
    "from utils import read_lines\n"
    "from stats import mean_length\n"
    "\n"
    "\n"
    "def main(path):\n"
    "    lines = read_lines(path)\n"
    "    print(len(lines), mean_length(lines))\n"
)
STATS = (
    "def mean_length(lines):\n"
    "    if not lines:\n"
    "        return 0.0\n"
    "    return sum(len(line) for line in lines) / len(lines)\n"
)

BASE: dict[str, bytes] = {
    "app.py": AUTHORS_APP.encode(),
    "utils.py": UTILS.encode(),
    "README.md": README.encode(),
}
PROJECT: dict[str, bytes] = {
    "app.py": STUDENTS_APP.encode(),
    "stats.py": STATS.encode(),
    "utils.py": UTILS.encode(),
    "README.md": README.encode(),
    ".env": b"OPENAI_API_KEY=sk-live-secret\n",
}

# Lines a "met" may stand on, and whose they are.
QUOTE_OF_NEW = "return sum(len(line) for line in lines) / len(lines)"
"""``stats.py`` line 4: the student's own, in a NEW file."""
QUOTE_OF_CHANGED = "print(len(lines), mean_length(lines))"
"""``app.py`` line 8: the student's line in a CHANGED file."""
QUOTE_OF_NEIGHBOR = 'with open(path, encoding="utf-8") as handle:'
"""``utils.py`` line 2: the author's, in a NEIGHBOR file."""
QUOTE_OF_AUTHORS_LINE = "lines = read_lines(path)"
"""``app.py`` line 7: the author's line, left in a CHANGED file."""

SECRET = "sk-live-secret"


async def read_recorded_project(monkeypatch: pytest.MonkeyPatch) -> DoorReading:
    """What the new path's door reads of the recorded project.

    The door's module logger is swapped for a fresh one: bound here
    (``cache_logger_on_first_use``), it would keep the processors of this test
    and slip past the ``capture_logs`` of the door's own tests.
    """
    monkeypatch.setattr(door_module, "logger", structlog.get_logger())
    monkeypatch.setattr(door_module, "project_context_budget_chars", lambda: 1_000_000)
    snapshot = normalize_archive(_zip_bytes(BASE), archive_kind="zip")
    stored = _FakeBase(
        version=1, snapshot_key="base/k", manifest=manifest_to_jsonb(snapshot.manifest)
    )
    monkeypatch.setattr(
        door_module, "ProjectBaseRepository", lambda _session: _FakeRepo(stored, stored)
    )
    reading = await process_project_submission(
        session=_FakeSession(),  # type: ignore[arg-type]
        s3=_FakeS3(base_snapshot_zip=snapshot.canonical_zip),  # type: ignore[arg-type]
        hw_repo=_FakeHwRepo(),  # type: ignore[arg-type]
        submission=_FakeSubmission(  # type: ignore[arg-type]
            base_id=uuid.uuid4(), authored_document_id=uuid.uuid4()
        ),
        sid=uuid.uuid4(),
        jid=uuid.uuid4(),
        file_bytes=_zip_bytes(PROJECT),
        raw_key="homework/t/s/proj.zip",
        languages=("ukr",),
        base_corpus=True,
    )
    assert reading is not None
    return reading
