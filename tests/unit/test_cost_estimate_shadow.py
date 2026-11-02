"""The shadow write never breaks the action it describes (BIL-D)."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from course_supporter.cost_estimate import shadow
from course_supporter.ingestion.text import TextProcessor
from course_supporter.models.source import SourceType
from course_supporter.service_logging import job_scope


def _broken(*_: Any) -> Any:
    raise RuntimeError("the estimate is broken on purpose")


class TestTheEstimateNeverBreaksTheAction:
    async def test_a_failing_computation_is_swallowed(self) -> None:
        factory = MagicMock()
        written = await shadow.record_estimate(
            _broken, action_key="k", session_factory=factory
        )
        assert written is False
        factory.assert_not_called()

    async def test_a_failing_write_is_swallowed(self) -> None:
        def factory() -> Any:
            raise ConnectionError("database down")

        written = await shadow.record_estimate(
            lambda s, at: _broken(), action_key="k", session_factory=factory
        )
        assert written is False

    async def test_no_factory_writes_nothing(self) -> None:
        shadow.configure(None)
        assert await shadow.record_estimate(_broken, action_key="k") is False

    async def test_the_processor_returns_its_document(self, tmp_path: Path) -> None:
        """A text job whose estimate raises still parses and returns its text."""
        source = tmp_path / "lesson.txt"
        source.write_text("First paragraph.\n\nSecond paragraph.", encoding="utf-8")
        material = SimpleNamespace(
            id=uuid.uuid4(),
            source_type=SourceType.TEXT,
            source_url=str(source),
            filename="lesson.txt",
        )
        shadow.configure(MagicMock())
        try:
            with (
                job_scope(uuid.uuid4()),
                patch(
                    "course_supporter.cost_estimate.hooks.estimate_processing",
                    side_effect=RuntimeError("broken on purpose"),
                ) as estimate,
            ):
                doc = await TextProcessor().process_raw(material)  # type: ignore[arg-type]
        finally:
            shadow.configure(None)
        estimate.assert_called_once()
        assert "Second paragraph." in doc.assemble_text()


@pytest.fixture(autouse=True)
def _no_factory_left_behind() -> Any:
    yield
    shadow.configure(None)
