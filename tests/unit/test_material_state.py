"""Tests for MaterialState derived property on MaterialEntry."""

from __future__ import annotations

from course_supporter.storage.orm import MaterialEntry, MaterialState, _uuid7


def _entry(**kwargs: object) -> MaterialEntry:
    """Create a MaterialEntry with sensible defaults, overriding with kwargs."""
    defaults: dict[str, object] = {
        "node_id": _uuid7(),
        "source_type": "web",
        "source_url": "https://example.com",
    }
    defaults.update(kwargs)
    return MaterialEntry(**defaults)  # type: ignore[arg-type]


class TestMaterialStateEnum:
    """MaterialState StrEnum values."""

    def test_values(self) -> None:
        """All five states have correct string values."""
        assert MaterialState.RAW == "raw"
        assert MaterialState.PENDING == "pending"
        assert MaterialState.READY == "ready"
        assert MaterialState.INTEGRITY_BROKEN == "integrity_broken"
        assert MaterialState.ERROR == "error"

    def test_is_str(self) -> None:
        """MaterialState values are strings (StrEnum)."""
        assert isinstance(MaterialState.RAW, str)


class TestStateRaw:
    """RAW state: uploaded, not processed, no pending job, no error."""

    def test_fresh_upload(self) -> None:
        """Freshly created entry with no processing is RAW."""
        entry = _entry()
        assert entry.state == MaterialState.RAW

    def test_raw_with_hash(self) -> None:
        """Entry with raw_hash but no processed content is RAW."""
        entry = _entry(raw_hash="a" * 64, raw_size_bytes=1024)
        assert entry.state == MaterialState.RAW


class TestStatePending:
    """PENDING state: ingestion job in flight."""

    def test_pending_with_job_id(self) -> None:
        """Entry with pending_job_id is PENDING."""
        entry = _entry(pending_job_id=_uuid7())
        assert entry.state == MaterialState.PENDING

    def test_pending_even_with_processed_content(self) -> None:
        """Re-processing: pending_job_id takes priority over existing content."""
        entry = _entry(
            pending_job_id=_uuid7(),
            processed_content='{"sections": []}',
            processed_hash="b" * 64,
        )
        assert entry.state == MaterialState.PENDING


class TestStateReady:
    """READY state: processed, hashes match (or no raw_hash to compare)."""

    def test_processed_no_raw_hash(self) -> None:
        """Processed content without raw_hash (URL source) is READY."""
        entry = _entry(
            processed_content='{"sections": []}',
            processed_hash="b" * 64,
        )
        assert entry.state == MaterialState.READY

    def test_processed_hashes_match(self) -> None:
        """Processed content with matching hashes is READY."""
        h = "a" * 64
        entry = _entry(
            raw_hash=h,
            processed_hash=h,
            processed_content='{"sections": []}',
        )
        assert entry.state == MaterialState.READY

    def test_processed_no_hashes(self) -> None:
        """Processed content with both hashes None is READY."""
        entry = _entry(processed_content='{"sections": []}')
        assert entry.state == MaterialState.READY


class TestStateIntegrityBroken:
    """INTEGRITY_BROKEN: raw changed after processing."""

    def test_hash_mismatch(self) -> None:
        """Different raw_hash vs processed_hash triggers INTEGRITY_BROKEN."""
        entry = _entry(
            raw_hash="a" * 64,
            processed_hash="b" * 64,
            processed_content='{"sections": []}',
        )
        assert entry.state == MaterialState.INTEGRITY_BROKEN

    def test_raw_hash_set_processed_hash_none(self) -> None:
        """raw_hash present but processed_hash None (re-upload after processing)."""
        entry = _entry(
            raw_hash="a" * 64,
            processed_hash=None,
            processed_content='{"sections": []}',
        )
        assert entry.state == MaterialState.INTEGRITY_BROKEN


class TestStateError:
    """ERROR state: processing failed."""

    def test_error_message_present(self) -> None:
        """error_message set means ERROR regardless of other fields."""
        entry = _entry(error_message="LLM timeout")
        assert entry.state == MaterialState.ERROR

    def test_error_takes_priority_over_pending(self) -> None:
        """ERROR > PENDING: error_message checked before pending_job_id."""
        entry = _entry(
            error_message="Processing crashed",
            pending_job_id=_uuid7(),
        )
        assert entry.state == MaterialState.ERROR

    def test_error_takes_priority_over_processed(self) -> None:
        """ERROR > READY: error_message checked before processed_content."""
        entry = _entry(
            error_message="Partial failure",
            processed_content='{"sections": []}',
            processed_hash="b" * 64,
        )
        assert entry.state == MaterialState.ERROR


class TestStatePriorityEdgeCases:
    """Priority edge cases between states."""

    def test_pending_takes_priority_over_raw(self) -> None:
        """PENDING > RAW: pending_job_id checked before processed_content."""
        entry = _entry(pending_job_id=_uuid7())
        assert entry.processed_content is None  # would be RAW without pending
        assert entry.state == MaterialState.PENDING

    def test_pending_takes_priority_over_integrity_broken(self) -> None:
        """PENDING > INTEGRITY_BROKEN: re-processing with hash mismatch."""
        entry = _entry(
            pending_job_id=_uuid7(),
            raw_hash="a" * 64,
            processed_hash="b" * 64,
            processed_content='{"sections": []}',
        )
        assert entry.state == MaterialState.PENDING

    def test_empty_error_message_is_not_error(self) -> None:
        """Empty string error_message is truthy — counts as ERROR."""
        entry = _entry(error_message="")
        # Empty string is falsy in Python, so this is NOT an error
        assert entry.state == MaterialState.RAW
