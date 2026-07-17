"""Unit tests for the KD18 P4 project-submission wiring.

The deep S3 / zip / burst-worker contour is the #4 integration test. Here we
assert that ``process_project_submission`` wires the pure ``build_mentor_context``
correctly — the staleness versions in both branches (base attached vs
``base_id=None``), the submission snapshot reused from memory while the base
snapshot is fetched once — and that the P3 interim builder and its budget
constant are gone. The builder itself is covered by ``test_mentor_context.py``.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from typing import Any

import pytest

from course_supporter.homework import project_submission as mod
from course_supporter.homework.project_submission import (
    _EMPTY_MANIFEST,
    _project_failure_reason,
    _submission_snapshot_key,
    process_project_submission,
)
from course_supporter.normalizer import manifest_to_jsonb, normalize_archive
from course_supporter.normalizer.exceptions import NormalizerLimitError
from course_supporter.security.exceptions import ErrorCategory, SecurityRejectedError

# ── fakes (no DB / no real S3) ─────────────────────────────────────────────


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


class _FakeS3:
    def __init__(self, base_snapshot_zip: bytes | None = None) -> None:
        self.uploaded: list[str] = []
        self.get_calls: list[str] = []
        self._base_zip = base_snapshot_zip

    async def upload_file(self, key: str, data: bytes, content_type: str) -> None:
        self.uploaded.append(key)

    async def get_object(self, key: str) -> bytes:
        self.get_calls.append(key)
        assert self._base_zip is not None
        return self._base_zip


class _FakeHwRepo:
    def __init__(self) -> None:
        self.stored: list[tuple[str, str]] = []

    async def store_snapshot(
        self,
        sid: uuid.UUID,
        *,
        snapshot_key: str,
        snapshot_hash: str,
        snapshot_manifest: dict[str, Any],
    ) -> None:
        self.stored.append((snapshot_key, snapshot_hash))


class _FakeSession:
    async def commit(self) -> None: ...


class _FakeBase:
    def __init__(
        self, *, version: int, snapshot_key: str, manifest: dict[str, Any]
    ) -> None:
        self.version = version
        self.snapshot_key = snapshot_key
        self.manifest = manifest


class _FakeRepo:
    def __init__(self, base: _FakeBase | None, latest: _FakeBase | None) -> None:
        self._base = base
        self._latest = latest

    async def get_by_id(self, base_id: uuid.UUID) -> _FakeBase | None:
        return self._base

    async def get_latest_ready(self, doc_id: uuid.UUID) -> _FakeBase | None:
        return self._latest


class _FakeSubmission:
    def __init__(
        self, *, base_id: uuid.UUID | None, authored_document_id: uuid.UUID
    ) -> None:
        self.base_id = base_id
        self.authored_document_id = authored_document_id
        self.original_filename = "proj.zip"


# ── wiring: base attached → real versions + one base fetch ─────────────────


async def test_wiring_base_attached_passes_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_build(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "RICH"

    monkeypatch.setattr(mod, "build_mentor_context", _fake_build)

    base_snap = normalize_archive(_zip_bytes({"a.py": b"x=1\n"}), archive_kind="zip")
    base_jsonb = manifest_to_jsonb(base_snap.manifest)
    base = _FakeBase(version=1, snapshot_key="base/k", manifest=base_jsonb)
    latest = _FakeBase(version=2, snapshot_key="base/k2", manifest=base_jsonb)
    monkeypatch.setattr(
        mod, "ProjectBaseRepository", lambda session: _FakeRepo(base, latest)
    )

    s3 = _FakeS3(base_snapshot_zip=base_snap.canonical_zip)
    submission = _FakeSubmission(
        base_id=uuid.uuid4(), authored_document_id=uuid.uuid4()
    )

    result = await process_project_submission(
        session=_FakeSession(),  # type: ignore[arg-type]
        s3=s3,  # type: ignore[arg-type]
        hw_repo=_FakeHwRepo(),  # type: ignore[arg-type]
        submission=submission,  # type: ignore[arg-type]
        sid=uuid.uuid4(),
        jid=uuid.uuid4(),
        file_bytes=_zip_bytes({"a.py": b"x=2\n", "b.py": b"y=1\n"}),
        raw_key="homework/t/sid/proj.zip",
    )

    assert result == "RICH"
    assert captured["base_version"] == 1
    assert captured["latest_version"] == 2
    assert captured["base_manifest"].included  # revived, non-empty base manifest
    # base snapshot fetched exactly once; submission snapshot came from memory.
    assert s3.get_calls == ["base/k"]
    assert s3.uploaded == ["homework/t/sid/snapshot.zip"]


# ── wiring: no base → versions None, base not fetched at all ────────────────


async def test_wiring_no_base_versions_none(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_build(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "RICH"

    monkeypatch.setattr(mod, "build_mentor_context", _fake_build)

    def _boom(session: Any) -> Any:
        raise AssertionError("ProjectBaseRepository must not run without a base")

    monkeypatch.setattr(mod, "ProjectBaseRepository", _boom)

    s3 = _FakeS3()  # no base zip; get_object must never be called
    submission = _FakeSubmission(base_id=None, authored_document_id=uuid.uuid4())

    result = await process_project_submission(
        session=_FakeSession(),  # type: ignore[arg-type]
        s3=s3,  # type: ignore[arg-type]
        hw_repo=_FakeHwRepo(),  # type: ignore[arg-type]
        submission=submission,  # type: ignore[arg-type]
        sid=uuid.uuid4(),
        jid=uuid.uuid4(),
        file_bytes=_zip_bytes({"only.py": b"z=1\n"}),
        raw_key="homework/t/sid/proj.zip",
    )

    assert result == "RICH"
    assert captured["base_version"] is None
    assert captured["latest_version"] is None
    assert captured["base_manifest"] is _EMPTY_MANIFEST
    assert s3.get_calls == []  # no base attached → no base fetch


# ── the P3 interim builder + its budget constant are gone ──────────────────


def test_interim_symbols_removed() -> None:
    assert not hasattr(mod, "build_interim_submission_text")
    assert not hasattr(mod, "PROJECT_SAFETY_TEXT_MAX_BYTES")


# ── retained helpers (unchanged by P4) ─────────────────────────────────────


class TestHelpers:
    def test_snapshot_key_is_sibling(self) -> None:
        assert (
            _submission_snapshot_key("homework/t/sid/proj.zip")
            == "homework/t/sid/snapshot.zip"
        )

    def test_failure_reason_security_prefixed_with_category(self) -> None:
        exc = SecurityRejectedError(ErrorCategory.ARCHIVE_BOMB, "too big")
        assert _project_failure_reason(exc) == "archive_bomb: too big"

    def test_failure_reason_normalizer_carries_class_name(self) -> None:
        exc = NormalizerLimitError("kept_total exceeded")
        reason = _project_failure_reason(exc)
        assert reason.startswith("NormalizerLimitError:")
        assert "kept_total exceeded" in reason
