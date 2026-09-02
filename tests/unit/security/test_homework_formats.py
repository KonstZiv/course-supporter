"""Every accepted homework format passes its conveyor with a real sample.

impl-rules#13: a fixture must be representative for the thing it exercises.
The formats here are decided by magic bytes and container structure, so a
byte stub would prove nothing -- these samples are produced by the same
builders the build-time image gate uses (``scripts/magic_format_gate.py``),
which generate genuine archives, documents and source files.

Sharing the builders with the gate is deliberate: the two checks answer
different questions -- the gate asks "does THIS IMAGE's libmagic see the
format", this module asks "does Stage 1 route the format to the pipeline the
table promises" -- and neither should drift into its own private idea of what
a ``.docx`` looks like.

Coverage is derived from ``HOMEWORK_POLICY.allowed_extensions``, so a format
added to the policy without a real sample fails here rather than shipping
unexercised.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from scripts.magic_format_gate import _builders

from course_supporter.security.exceptions import SecurityRejectedError
from course_supporter.security.policies import HOMEWORK_CONVEYORS, HOMEWORK_POLICY
from course_supporter.security.stage1 import Stage1Result, run_stage1

ACCEPTED = sorted(HOMEWORK_POLICY.allowed_extensions)


@pytest.fixture
def sample(tmp_path: Path) -> Callable[[str], bytes]:
    """Build a real file for ``ext`` and return its bytes."""

    def _build(ext: str) -> bytes:
        builders = _builders(tmp_path)
        assert ext in builders, (
            f"no real sample builder for {ext!r}; the policy admits a format "
            f"the suite cannot exercise (see scripts/magic_format_gate.py)"
        )
        target = tmp_path / f"work.{ext}"
        builders[ext](target)
        return target.read_bytes()

    return _build


@pytest.mark.parametrize("ext", ACCEPTED)
def test_accepted_format_passes_stage1(
    ext: str, sample: Callable[[str], bytes]
) -> None:
    content = sample(ext)
    try:
        result = run_stage1(filename=f"work.{ext}", content=content, context="homework")
    except SecurityRejectedError as exc:  # pragma: no cover - failure detail
        pytest.fail(
            f"{ext!r} is accepted by HOMEWORK_POLICY but Stage 1 rejected a "
            f"real sample: {exc.category.value}: {exc.detail}"
        )
    assert isinstance(result, Stage1Result)
    assert result.extension == ext


@pytest.mark.parametrize("ext", ACCEPTED)
def test_accepted_format_reaches_its_conveyor(
    ext: str, sample: Callable[[str], bytes]
) -> None:
    result = run_stage1(filename=f"work.{ext}", content=sample(ext), context="homework")
    conveyor = HOMEWORK_CONVEYORS[ext]

    if conveyor == "text":
        assert result.nfc_text is not None, (
            f"{ext!r} is routed to the text conveyor but Stage 1 produced no "
            f"NFC text — the content checks did not run on it"
        )
        assert result.archive_entries is None
    elif conveyor == "archive":
        assert result.archive_entries is not None, (
            f"{ext!r} is routed to the archive conveyor but Stage 1 produced no entries"
        )
        assert {entry.arcname for entry in result.archive_entries} == {
            "main.py",
            "README.md",
        }
    else:
        # The document conveyor itself lands with the extraction work; today
        # Stage 1 only has to accept the file and identify it. Tightening this
        # branch to assert extracted text is what proves that conveyor exists.
        assert conveyor == "document"
        assert result.detected_mime is not None
        assert result.nfc_text is None
