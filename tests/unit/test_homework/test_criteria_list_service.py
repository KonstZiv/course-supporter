"""Unit tests for the pure parts of the criteria-list service (task 08, K3).

The rule of the list in force, the input fingerprint and the timings of the
claim protocol — on objects built in memory, no session. The claim protocol
itself runs against a real database in ``test_criteria_list_service_db.py``.
"""

from __future__ import annotations

import doctest
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.criteria_list_state import CriteriaListState
from course_supporter.homework import criteria_list_service
from course_supporter.homework.criteria_list_service import (
    CLAIM_POLL_INTERVAL,
    CLAIM_SILENCE_LIMIT,
    CLAIM_WAIT_LIMIT,
    HEARTBEAT_INTERVAL,
    choose_in_force,
    input_fingerprint,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    TaskCriteriaList,
    TaskCriteriaOverride,
)

_HASH = "a" * 64
_OTHER_HASH = "b" * 64


def _criterion(text: str) -> dict[str, Any]:
    return {
        "id": "c1",
        "text": text,
        "evidence": "evidence",
        "weight": "must",
        "check_method": "model_verdict",
        "soft_descent": False,
        "concepts": [],
        "mandatory_points": [],
    }


def _document(
    *, content_hash: str = _HASH, task_type: str = "task"
) -> AuthoredDocument:
    return AuthoredDocument(
        id=uuid.uuid4(), content_hash=content_hash, task_type=task_type
    )


def _machine(
    *,
    state: str = CriteriaListState.READY.value,
    content_hash: str = _HASH,
    task_type: str = "task",
    deleted_at: datetime | None = None,
) -> TaskCriteriaList:
    ready = state == CriteriaListState.READY.value
    return TaskCriteriaList(
        id=uuid.uuid4(),
        source_content_hash=content_hash,
        source_task_type=task_type,
        state=state,
        criteria=[_criterion("from the model")] if ready else None,
        deleted_at=deleted_at,
    )


def _override(
    *,
    content_hash: str = _HASH,
    task_type: str = "task",
    deleted_at: datetime | None = None,
) -> TaskCriteriaOverride:
    return TaskCriteriaOverride(
        id=uuid.uuid4(),
        source_content_hash=content_hash,
        source_task_type=task_type,
        criteria=[_criterion("from the author")],
        deleted_at=deleted_at,
    )


class TestChooseInForce:
    def test_the_authors_edit_for_this_version_wins(self) -> None:
        override = _override()
        chosen = choose_in_force(_document(), machine=_machine(), override=override)

        assert chosen is not None
        assert chosen.layer is CriteriaLayer.AUTHOR
        assert chosen.source_id == override.id
        assert chosen.criteria[0].text == "from the author"

    def test_the_authors_edit_needs_no_model_list(self) -> None:
        chosen = choose_in_force(_document(), machine=None, override=_override())
        assert chosen is not None
        assert chosen.layer is CriteriaLayer.AUTHOR

    @pytest.mark.parametrize(
        "override",
        [
            pytest.param(_override(content_hash=_OTHER_HASH), id="earlier-content"),
            pytest.param(_override(task_type="project"), id="earlier-type"),
            pytest.param(_override(deleted_at=datetime.now(UTC)), id="reset"),
        ],
    )
    def test_an_edit_not_of_this_version_gives_way_to_the_model(
        self, override: TaskCriteriaOverride
    ) -> None:
        machine = _machine()
        chosen = choose_in_force(_document(), machine=machine, override=override)

        assert chosen is not None
        assert chosen.layer is CriteriaLayer.MODEL
        assert chosen.source_id == machine.id

    @pytest.mark.parametrize(
        "machine",
        [
            pytest.param(_machine(state=CriteriaListState.PENDING.value), id="claim"),
            pytest.param(_machine(content_hash=_OTHER_HASH), id="stale-content"),
            pytest.param(_machine(task_type="project"), id="stale-type"),
            pytest.param(_machine(deleted_at=datetime.now(UTC)), id="history"),
        ],
    )
    def test_no_list_in_force_without_a_ready_list_of_this_version(
        self, machine: TaskCriteriaList
    ) -> None:
        assert choose_in_force(_document(), machine=machine, override=None) is None

    def test_nothing_at_all_means_no_list(self) -> None:
        assert choose_in_force(_document(), machine=None, override=None) is None


class TestInputFingerprint:
    _BASE: dict[str, Any] = {  # noqa: RUF012
        "content_hash": _HASH,
        "node_description": "Recursion in Python.",
        "node_concepts": ["Recursion"],
        "root_concepts": ["Functions"],
        "prompt_hash": "p" * 64,
    }

    def test_the_same_input_gives_the_same_fingerprint(self) -> None:
        first = input_fingerprint(**self._BASE)
        assert first == input_fingerprint(**self._BASE)
        assert len(first) == 64
        int(first, 16)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("content_hash", _OTHER_HASH),
            ("node_description", "Loops."),
            ("node_concepts", ["Recursion", "Base Case"]),
            ("root_concepts", []),
            ("prompt_hash", "q" * 64),
        ],
    )
    def test_every_part_of_the_input_counts(self, field: str, value: object) -> None:
        changed = {**self._BASE, field: value}
        assert input_fingerprint(**changed) != input_fingerprint(**self._BASE)

    def test_node_and_root_concepts_are_not_interchangeable(self) -> None:
        swapped = {
            **self._BASE,
            "node_concepts": self._BASE["root_concepts"],
            "root_concepts": self._BASE["node_concepts"],
        }
        assert input_fingerprint(**swapped) != input_fingerprint(**self._BASE)


def test_a_live_claim_cannot_look_abandoned() -> None:
    """The timings only work together.

    A live claimer misses a beat or two, never several in a row, so the silence
    that abandons a claim must span several heartbeats; and a waiting submission
    must outlast that silence to notice a claimer that is gone.
    """
    assert CLAIM_SILENCE_LIMIT >= 3 * HEARTBEAT_INTERVAL
    assert CLAIM_WAIT_LIMIT > CLAIM_SILENCE_LIMIT
    assert CLAIM_POLL_INTERVAL < CLAIM_SILENCE_LIMIT


def test_the_module_examples_run() -> None:
    """The docstring examples are executed, not prose (task 09b adds the first)."""
    result = doctest.testmod(criteria_list_service)

    assert result.attempted > 0, "the module has examples to run"
    assert result.failed == 0
