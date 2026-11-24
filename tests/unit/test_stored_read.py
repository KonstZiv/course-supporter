"""A stored row is read leniently, written strictly (debt DD-SP-DL).

``extra="forbid"`` on the models of a review stays: it keeps a writer from
putting in a field nobody reads. What this pins is the other half: a row that
holds a field a *newer* release wrote is read without that field, and every
other defect is still a defect.
"""

from __future__ import annotations

import copy
import doctest
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from structlog.testing import capture_logs

from course_supporter.homework.verdict_explanation import (
    CriterionRemark,
    ExplanationAnswer,
)
from course_supporter.models import review_schema, review_structure, stored_read
from course_supporter.models.review_structure import (
    PassRule,
    Position,
    Reference,
    Remark,
    Reply,
    ReviewStructureV1,
    Verdict,
    Verification,
)
from course_supporter.models.stored_read import read_stored
from tests._helpers.stored_review import (
    MARKER,
    REVIEW_PLACES,
    UNKNOWN,
    a_review,
    dotted,
    stored_explanation,
    stored_review,
    with_unknown,
)

_IGNORED = "stored_unknown_fields_ignored"


def _kinds(error: ValidationError) -> list[tuple[str, tuple[str | int, ...]]]:
    """What is wrong, as pydantic names it: the kind and the place, no values."""
    return [(item["type"], item["loc"]) for item in error.errors(include_input=False)]


class TestAnUnknownFieldIsLeftOut:
    @pytest.mark.parametrize(
        "place", list(REVIEW_PLACES.values()), ids=list(REVIEW_PLACES)
    )
    def test_at_every_shape_of_the_models_loc_is_the_path_in_the_data(
        self, place: tuple[str | int, ...]
    ) -> None:
        """The check the pruning rests on: ``loc`` names the field's place in the
        data for a nested model, ``Model | None``, and a list of models alike.
        Were it not so, the read would fail, not guess."""
        stored = stored_review()

        with capture_logs() as logs:
            read = read_stored(ReviewStructureV1, with_unknown(stored, place))

        assert read == a_review()
        assert [entry["fields"] for entry in logs] == [[dotted(place)]]

    def test_every_field_of_a_review_at_once(self) -> None:
        stored = stored_review()
        places = list(REVIEW_PLACES.values())

        with capture_logs() as logs:
            read = read_stored(ReviewStructureV1, with_unknown(stored, *places))

        assert read == a_review()
        (entry,) = logs
        assert sorted(entry["fields"]) == sorted(dotted(place) for place in places)

    def test_in_a_tuple_of_models_too(self) -> None:
        """An explanation keeps its remarks in a tuple, not a list."""
        stored = stored_explanation()

        with capture_logs() as logs:
            read = read_stored(
                ExplanationAnswer,
                with_unknown(stored, (), ("remarks", 0), ("remarks", 1)),
            )

        assert read == ExplanationAnswer.model_validate(stored)
        (entry,) = logs
        assert sorted(entry["fields"]) == sorted(
            [UNKNOWN, f"remarks.0.{UNKNOWN}", f"remarks.1.{UNKNOWN}"]
        )

    def test_the_rest_of_the_row_is_read_as_stored(self) -> None:
        stored = stored_review()

        read = read_stored(
            ReviewStructureV1, with_unknown(stored, *REVIEW_PLACES.values())
        )

        assert read.model_dump(mode="json") == stored

    def test_the_stored_mapping_is_not_changed(self) -> None:
        noisy = with_unknown(stored_review(), *REVIEW_PLACES.values())
        before = copy.deepcopy(noisy)

        read_stored(ReviewStructureV1, noisy)

        assert noisy == before

    def test_a_row_with_nothing_unknown_is_read_silently(self) -> None:
        with capture_logs() as logs:
            read = read_stored(ReviewStructureV1, stored_review())

        assert read == a_review()
        assert logs == []


# Each: what is wrong with a stored review, other than an unknown field.
_DEFECTS: dict[str, Callable[[dict[str, Any]], object]] = {
    "a required field is missing": lambda row: row["new_remarks"][0].pop("what"),
    "the language is missing": lambda row: row.pop("language"),
    "a required field of the wrong type": lambda row: row.update(language=5),
    "an optional field of the wrong type": lambda row: row.update(progress=5),
    "an optional field out of its range": lambda row: row.update(
        unchanged_from_attempt=0
    ),
    "a field that breaks its pattern": lambda row: row["new_remarks"][0].update(
        item_id="not an id"
    ),
    "a rule of the model broken": lambda row: row["verdict"].update(passed=False),
}


class TestAnyOtherDefectIsStillOne:
    @pytest.mark.parametrize("defect", list(_DEFECTS), ids=list(_DEFECTS))
    def test_alone_it_is_the_error_the_model_gives(self, defect: str) -> None:
        stored = stored_review()
        _DEFECTS[defect](stored)

        with pytest.raises(ValidationError) as strict:
            ReviewStructureV1.model_validate(stored)
        with pytest.raises(ValidationError) as read:
            read_stored(ReviewStructureV1, stored)

        assert _kinds(read.value) == _kinds(strict.value)

    @pytest.mark.parametrize("defect", list(_DEFECTS), ids=list(_DEFECTS))
    def test_next_to_an_unknown_field_nothing_is_taken_out(self, defect: str) -> None:
        """Only unknown fields are left out, and only when they are all that is
        wrong: an unknown field next to a real defect does not make the row
        readable, and nothing is said about fields skipped."""
        stored = stored_review()
        _DEFECTS[defect](stored)
        noisy = with_unknown(stored, (), ("new_remarks", 1), ("verdict", "rule"))

        with capture_logs() as logs, pytest.raises(ValidationError):
            read_stored(ReviewStructureV1, noisy)

        assert logs == []

    def test_a_defect_that_shows_only_without_the_unknown_field_is_one_too(
        self,
    ) -> None:
        """A reference must point somewhere; the model checks that only once its
        own fields are known. The unknown field goes, the rule then refuses."""
        stored = stored_review()
        stored["new_remarks"][0]["read"] = [{"title": "Secrets in code", UNKNOWN: 1}]

        with capture_logs() as logs, pytest.raises(ValidationError) as read:
            read_stored(ReviewStructureV1, stored)

        assert "points somewhere" in str(read.value)
        assert logs == []

    def test_a_path_the_data_does_not_have_is_not_guessed_at(self) -> None:
        """If ``loc`` named a place the stored data has no field at, the row is
        left as the error it is, and nothing is taken out of anything."""

        class Rewrites(BaseModel):
            model_config = ConfigDict(extra="forbid")

            x: int

            @model_validator(mode="before")
            @classmethod
            def _adds_a_key(cls, data: Any) -> Any:
                return {**data, "added": 1}

        with pytest.raises(ValidationError) as read:
            read_stored(Rewrites, {"x": 1})

        assert _kinds(read.value) == [("extra_forbidden", ("added",))]


class TestTheLogLine:
    def test_it_names_the_model_and_the_paths_and_the_submission(self) -> None:
        submission_id = uuid.uuid4()
        noisy = with_unknown(
            stored_review(), (), ("new_remarks", 1), ("verdict", "rule")
        )

        with capture_logs() as logs:
            read_stored(ReviewStructureV1, noisy, submission_id=submission_id)

        (entry,) = logs
        assert entry["event"] == _IGNORED
        assert entry["log_level"] == "warning"
        assert entry["model"] == "ReviewStructureV1"
        assert sorted(entry["fields"]) == sorted(
            [UNKNOWN, f"new_remarks.1.{UNKNOWN}", f"verdict.rule.{UNKNOWN}"]
        )
        assert entry["submission_id"] == str(submission_id)

    def test_without_a_submission_it_has_no_such_key(self) -> None:
        with capture_logs() as logs:
            read_stored(ReviewStructureV1, with_unknown(stored_review(), ()))

        assert "submission_id" not in logs[0]

    def test_it_never_carries_the_value_of_a_field_it_left_out(self) -> None:
        """The value is the student's work or the Mentor's words about it."""
        noisy = with_unknown(stored_review(), *REVIEW_PLACES.values())

        with capture_logs() as logs:
            read_stored(ReviewStructureV1, noisy, submission_id=uuid.uuid4())

        assert logs
        assert MARKER not in repr(logs)

    def test_it_is_one_line_however_many_fields_there_are(self) -> None:
        noisy = with_unknown(stored_review(), *REVIEW_PLACES.values())

        with capture_logs() as logs:
            read_stored(ReviewStructureV1, noisy)

        assert [entry["event"] for entry in logs] == [_IGNORED]


# Every model whose rows are read back from storage by the readers of the debt,
# and which writes depend on being strict.
_STRICT_MODELS: list[type[BaseModel]] = [
    ReviewStructureV1,
    review_schema.VersionedReview,
    Remark,
    Reference,
    Position,
    Verdict,
    PassRule,
    Reply,
    Verification,
    review_structure.TestSection,
    review_structure.TestQuestionResult,
    review_structure.TestOption,
    ExplanationAnswer,
    CriterionRemark,
]


class TestWritingStaysStrict:
    @pytest.mark.parametrize("model", _STRICT_MODELS, ids=lambda m: m.__name__)
    def test_the_model_still_forbids_what_it_does_not_know(
        self, model: type[BaseModel]
    ) -> None:
        assert model.model_config.get("extra") == "forbid"

    def test_building_a_review_with_an_unknown_field_is_refused(self) -> None:
        with pytest.raises(ValidationError) as refused:
            ReviewStructureV1(
                schema_version="1",
                language="ukr",
                progress="Well done.",
                **{UNKNOWN: MARKER},  # type: ignore[arg-type]
            )

        assert _kinds(refused.value) == [("extra_forbidden", (UNKNOWN,))]

    def test_validating_one_directly_is_refused(self) -> None:
        with pytest.raises(ValidationError) as refused:
            ReviewStructureV1.model_validate(
                with_unknown(stored_review(), ("verdict",))
            )

        assert _kinds(refused.value) == [("extra_forbidden", ("verdict", UNKNOWN))]

    def test_a_remark_of_the_explanation_is_refused_too(self) -> None:
        with pytest.raises(ValidationError) as refused:
            ExplanationAnswer.model_validate(
                with_unknown(stored_explanation(), ("remarks", 0))
            )

        assert _kinds(refused.value) == [("extra_forbidden", ("remarks", 0, UNKNOWN))]


class TestDocstrings:
    def test_the_examples_in_the_module_run(self) -> None:
        """Doctests are not collected by the gate; this runs the module's."""
        with capture_logs():
            results = doctest.testmod(
                stored_read,
                optionflags=doctest.ELLIPSIS | doctest.IGNORE_EXCEPTION_DETAIL,
            )

        assert results.failed == 0
        assert results.attempted > 0
