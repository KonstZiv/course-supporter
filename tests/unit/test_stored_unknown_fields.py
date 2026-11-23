"""The readers of a stored review and explanation skip what they do not know.

The debt DD-SP-DL, at its readers: ``curated_structure`` (the portal and the
webhook), ``_version_1_review`` (the rule of the copy) and the two reads of the
stored explanation in ``text_result``. Each is given a row that holds a field a
newer release wrote, and each must read it as the row it is; and each is given a
row with a real defect and must refuse it as it always did.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from course_supporter.api.routes._portal_shared import (
    curated_structure,
    curated_verdict,
)
from course_supporter.api.schemas import PortalVerdict
from course_supporter.homework import text_result
from course_supporter.homework.attempt_dedup import _version_1_review
from course_supporter.homework.webhook import build_reviewed_payload
from course_supporter.models.review_structure import ReviewStructureV1
from course_supporter.remark_kinds import RemarkState
from tests._helpers.stored_review import (
    MARKER,
    UNKNOWN,
    a_review,
    at,
    dotted,
    stored_explanation,
    stored_review,
    with_unknown,
)

_IGNORED = "stored_unknown_fields_ignored"
_INVALID = "review_structure_invalid"

# The three levels the debt names: the root, a remark (``new_remarks[i]`` and
# ``open[i]``), a model inside a model (``verdict.rule``).
_LEVELS: dict[str, tuple[str | int, ...]] = {
    "the root": (),
    "a new remark": ("new_remarks", 1),
    "an open remark": ("open", 0),
    "the rule of the verdict": ("verdict", "rule"),
}


def _keys(node: object) -> set[str]:
    """Every key anywhere in a dumped review."""
    if isinstance(node, dict):
        return set(node) | {key for value in node.values() for key in _keys(value)}
    if isinstance(node, list):
        return {key for item in node for key in _keys(item)}
    return set()


def _submission(review_result: dict[str, Any] | None, *, score: int = 90) -> MagicMock:
    submission = MagicMock()
    submission.id = uuid.uuid4()
    submission.score = score
    submission.review_markdown = "## Good work"
    submission.response_language = "uk"
    submission.review_result = review_result
    submission.created_at = datetime.now(UTC)
    return submission


class TestCuratedStructure:
    @pytest.mark.parametrize("level", list(_LEVELS), ids=list(_LEVELS))
    def test_a_version_1_review_with_an_unknown_field_is_read(self, level: str) -> None:
        stored = stored_review()

        structure = curated_structure(with_unknown(stored, _LEVELS[level]))

        assert structure == a_review()

    def test_no_unknown_field_is_in_the_result_and_the_rest_is_as_stored(self) -> None:
        stored = stored_review()

        structure = curated_structure(with_unknown(stored, *_LEVELS.values()))

        assert structure is not None
        dumped = structure.model_dump(mode="json")
        assert UNKNOWN not in _keys(dumped)
        assert MARKER not in repr(dumped)
        assert dumped == stored

    def test_the_remarks_and_the_verdict_are_there_not_an_empty_review(self) -> None:
        structure = curated_structure(with_unknown(stored_review(), *_LEVELS.values()))

        assert structure is not None
        assert structure.verdict is not None
        assert structure.verdict.passed is True
        assert structure.verdict.rule is not None
        assert [len(structure.new_remarks), len(structure.open)] == [2, 1]

    @pytest.mark.parametrize(
        "defect",
        [
            lambda row: row["new_remarks"][0].pop("what"),
            lambda row: row.update(language="not-a-code"),
            lambda row: row["verdict"].update(passed="maybe"),
            lambda row: row.update(progress=5),
        ],
        ids=["missing field", "bad value", "wrong type", "optional of wrong type"],
    )
    def test_another_defect_reads_as_before_none_and_the_old_log_line(
        self, defect: Any
    ) -> None:
        """With an unknown field or without: the row is bad data as it was."""
        for with_the_unknown_field in (False, True):
            stored = stored_review()
            defect(stored)
            if with_the_unknown_field:
                stored = with_unknown(stored, *_LEVELS.values())

            with capture_logs() as logs:
                structure = curated_structure(stored)

            assert structure is None
            assert [entry["event"] for entry in logs] == [_INVALID]

    def test_the_line_of_a_skipped_field_has_paths_and_never_values(self) -> None:
        submission_id = uuid.uuid4()
        noisy = with_unknown(stored_review(), *_LEVELS.values())

        with capture_logs() as logs:
            curated_structure(noisy, submission_id=submission_id)

        (entry,) = logs
        assert entry["event"] == _IGNORED
        assert entry["model"] == "ReviewStructureV1"
        assert sorted(entry["fields"]) == sorted(
            dotted(level) for level in _LEVELS.values()
        )
        assert entry["submission_id"] == str(submission_id)
        assert MARKER not in repr(logs)

    def test_a_review_without_unknown_fields_is_read_in_silence(self) -> None:
        with capture_logs() as logs:
            assert curated_structure(stored_review()) == a_review()

        assert logs == []

    def test_a_trace_and_another_version_are_still_not_a_structure(self) -> None:
        trace = {"verdict": {"passed": True}, UNKNOWN: MARKER}
        future = {**stored_review(), "schema_version": "2", UNKNOWN: MARKER}

        with capture_logs() as logs:
            assert curated_structure(trace) is None
            assert curated_structure(future) is None

        assert logs == []

    def test_the_verdict_of_the_portal_reads_through_it(self) -> None:
        noisy = with_unknown(stored_review(), *_LEVELS.values())

        verdict = curated_verdict(noisy, score=90)

        assert verdict == PortalVerdict(passed=True, correctness="partially_correct")


class TestTheWebhook:
    def test_passed_is_the_stored_one_not_false(self) -> None:
        """The failure the debt names: an unknown field made ``passed=False``."""
        student = MagicMock()
        student.external_id = "e-1"
        noisy = with_unknown(stored_review(), *_LEVELS.values())

        payload = build_reviewed_payload(_submission(noisy), student)

        assert payload.review.passed is True
        assert payload.structure == a_review()
        assert UNKNOWN not in _keys(payload.model_dump(mode="json"))

    def test_a_stored_failure_stays_a_failure(self) -> None:
        stored = stored_review()
        stored["verdict"] = {"passed": False, "why": "A requirement is not met."}
        student = MagicMock()
        student.external_id = "e-1"

        payload = build_reviewed_payload(
            _submission(with_unknown(stored, ("verdict",))), student
        )

        assert payload.review.passed is False
        assert payload.structure is not None


class TestTheRuleOfTheCopy:
    def test_a_source_with_an_unknown_field_is_a_source(self) -> None:
        noisy = with_unknown(stored_review(), *_LEVELS.values())

        assert _version_1_review(noisy) == a_review()

    def test_it_names_the_source_in_the_log(self) -> None:
        source_id = uuid.uuid4()

        with capture_logs() as logs:
            _version_1_review(
                with_unknown(stored_review(), ("verdict",)), submission_id=source_id
            )

        (entry,) = logs
        assert entry["event"] == _IGNORED
        assert entry["fields"] == [f"verdict.{UNKNOWN}"]
        assert entry["submission_id"] == str(source_id)

    @pytest.mark.parametrize(
        "defect",
        [
            lambda row: row["new_remarks"][0].pop("what"),
            lambda row: row.update(language=5),
        ],
        ids=["missing field", "wrong type"],
    )
    def test_another_defect_is_no_source_as_before(self, defect: Any) -> None:
        stored = with_unknown(stored_review(), ("verdict",))
        defect(stored)

        assert _version_1_review(stored) is None

    def test_what_is_not_a_version_1_review_is_none_as_before(self) -> None:
        assert _version_1_review(None) is None
        assert _version_1_review({}) is None
        assert _version_1_review({"verdict": {"passed": True}}) is None
        assert (
            _version_1_review(
                {**with_unknown(stored_review(), ()), "schema_version": "2"}
            )
            is None
        )


class TestTheStoredExplanation:
    """``ExplanationAnswer`` is read back twice in ``text_result``."""

    def test_a_remark_with_an_unknown_field_is_read(self) -> None:
        stored = stored_explanation()

        read = text_result.read_stored(
            text_result.ExplanationAnswer,
            with_unknown(stored, (), ("remarks", 0), ("remarks", 1)),
        )

        assert read == text_result.ExplanationAnswer.model_validate(stored)
        assert [remark.id for remark in read.remarks] == ["c1", "c2"]

    async def test_the_words_of_a_fixed_criterion_come_from_such_an_explanation(
        self,
    ) -> None:
        """The first read: the earlier attempt's remarks, for a fixed criterion."""
        earlier = SimpleNamespace(submission_id=uuid.uuid4())
        explained = SimpleNamespace(
            body=with_unknown(stored_explanation(), ("remarks", 0), ("remarks", 1))
        )
        verdicts = MagicMock()
        verdicts.earlier_attempt = AsyncMock(return_value=earlier)
        verdicts.get_explanation = AsyncMock(return_value=explained)
        submission = SimpleNamespace(tenant_id=uuid.uuid4(), id=uuid.uuid4())

        with (
            patch.object(
                text_result, "SubmissionVerdictRepository", return_value=verdicts
            ),
            patch.object(text_result, "read_judged_list", AsyncMock(return_value=())),
            capture_logs() as logs,
        ):
            fixed = await text_result._fixed_words(
                MagicMock(),
                submission,  # type: ignore[arg-type]
                [],
                states={"c1": RemarkState.FIXED},
            )

        assert fixed is not None
        assert sorted(fixed.remarks) == ["c1", "c2"]
        assert fixed.remarks["c1"].what == "The test for an empty list is missing."
        (entry,) = logs
        assert entry["event"] == _IGNORED
        assert entry["model"] == "ExplanationAnswer"
        assert entry["submission_id"] == str(earlier.submission_id)
        assert MARKER not in repr(logs)

    def test_an_explanation_with_another_defect_is_still_refused(self) -> None:
        stored = with_unknown(stored_explanation(), ("remarks", 0))
        del stored["remarks"][1]["why"]

        with pytest.raises(ValueError, match="why"):
            text_result.read_stored(text_result.ExplanationAnswer, stored)


def test_the_review_a_row_holds_is_the_one_the_model_wrote() -> None:
    """The fixture is no looser than the model: it validates strictly as it is."""
    assert ReviewStructureV1.model_validate(stored_review()) == a_review()
    assert at(stored_review(), ("verdict", "rule", "basis")) == "author_musts_met"
