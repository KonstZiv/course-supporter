"""The readers of stored JSON skip what they do not know (DD-SP-DN).

The debt DD-SP-DL, at the readers it did not reach: a model with
``extra="forbid"`` refuses a row a newer release wrote once production is rolled
back to an older one. Each reader below is given a row that holds a field its
model does not know — and must read it as the row it is — and a row with a real
defect, which it must refuse as it always did.

The models stay strict for writing: ``model_validate`` of each still refuses the
unknown field (the first test of every class).
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from pydantic import ValidationError
from structlog.testing import capture_logs

from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.homework import (
    course_link_delivery,
    judged_list,
    path_checkpoint,
    review_explanation,
    text_result,
)
from course_supporter.homework.course_links import CriterionLinks, FoundCourseLinks
from course_supporter.homework.criteria_form import (
    Criterion,
    MandatoryPoint,
    author_criteria_from_document,
    criteria_from_document,
)
from course_supporter.homework.path_checkpoint import PathCheckpoint
from course_supporter.homework.path_config import PathKey, SubmissionState
from course_supporter.homework.path_continuation import _checkpoint_of
from course_supporter.homework.test_object import (
    DraftBody,
    DraftOption,
    DraftQuestion,
    PublishedBody,
    published_form,
)
from course_supporter.models import stored_read
from course_supporter.models.source import AssignmentType
from course_supporter.storage.node_summary_run_state import NodeSummaryRunState
from tests._helpers.stored_review import MARKER, UNKNOWN, with_unknown

_IGNORED = "stored_unknown_fields_ignored"


@pytest.fixture(autouse=True)
def _fresh_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reader's logger swapped for a fresh one.

    A logger bound by an earlier test is cached (``cache_logger_on_first_use``)
    with the processors of that test and would slip past ``capture_logs``.
    """
    monkeypatch.setattr(stored_read, "logger", structlog.get_logger())


def _the_line(logs: list[dict[str, Any]]) -> dict[str, Any]:
    (line,) = [entry for entry in logs if entry["event"] == _IGNORED]
    assert MARKER not in repr(logs)
    return line


# ── FoundCourseLinks: three readers ─────────────────────────────────────────


def _found() -> dict[str, Any]:
    return FoundCourseLinks(
        criteria=(
            CriterionLinks(item_id="c1", candidates=()),
            CriterionLinks(item_id="c2", candidates=()),
        ),
        settled=(CriterionLinks(item_id="c1", candidates=()),),
    ).model_dump(mode="json")


_FOUND_PLACES: dict[str, tuple[str | int, ...]] = {
    "the root": (),
    "a place of criteria": ("criteria", 1),
    "a place of settled": ("settled", 0),
}


def _submission() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        course_node_id=uuid.uuid4(),
        authored_document_id=uuid.uuid4(),
    )


def _repository(body: dict[str, Any]) -> MagicMock:
    verdicts = MagicMock()
    verdicts.get_candidates = AsyncMock(return_value=SimpleNamespace(body=body))
    verdicts.list_for_submission = AsyncMock(return_value=[MagicMock()])
    return verdicts


class _StoppedError(Exception):
    """Raised by a double to stop a reader once it has read what it reads."""

    def __init__(self, found: FoundCourseLinks) -> None:
        super().__init__()
        self.found = found


async def _read_by_the_delivery(body: dict[str, Any]) -> FoundCourseLinks | dict:
    """``settle_course_links`` reads the row first, and with ``settled`` stops."""
    submission = _submission()
    with patch.object(
        course_link_delivery,
        "SubmissionVerdictRepository",
        return_value=_repository(body),
    ):
        settled = await course_link_delivery.settle_course_links(
            MagicMock(),
            MagicMock(),
            submission,  # type: ignore[arg-type]
            MagicMock(),
            [],
        )
    return settled


async def _read_by_the_explanation_input(body: dict[str, Any]) -> FoundCourseLinks:
    """``explanation_input`` hands the row it read to ``links_to_show``."""
    submission = _submission()

    def _stop(_unmet: object, found: FoundCourseLinks) -> None:
        raise _StoppedError(found)

    with (
        patch.object(
            review_explanation,
            "SubmissionVerdictRepository",
            return_value=_repository(body),
        ),
        patch.object(
            review_explanation, "read_judged_list", AsyncMock(return_value=())
        ),
        patch.object(review_explanation, "judged_layer", MagicMock()),
        patch.object(review_explanation, "explanation_facts", MagicMock()),
        patch.object(review_explanation, "links_to_show", _stop),
        pytest.raises(_StoppedError) as stopped,
    ):
        await review_explanation.explanation_input(
            MagicMock(),
            submission,  # type: ignore[arg-type]
            submission_text="work",
            language="ukr",
        )
    return stopped.value.found


async def _read_after_delivery(body: dict[str, Any]) -> float | None:
    """``after_delivery`` counts the course links of the criteria not met."""
    submission = _submission()
    unmet = SimpleNamespace(
        item_id="c1", item_kind="criterion", verdict="not_met", weight="must"
    )
    recorded = AsyncMock()
    verdicts = _repository(body)
    verdicts.list_for_submission = AsyncMock(return_value=[unmet])
    with (
        patch.object(text_result, "SubmissionVerdictRepository", return_value=verdicts),
        patch.object(text_result, "HomeworkCostRepository") as costs,
        patch.object(text_result, "record_review_metrics", recorded),
    ):
        costs.return_value.has_review_metrics = AsyncMock(return_value=False)
        context = SimpleNamespace(
            session=MagicMock(), session_factory=MagicMock(), submission=submission
        )
        await text_result.TextResultBuilder().after_delivery(context)  # type: ignore[arg-type]
    (metrics,) = [call.args[1] for call in recorded.await_args_list]
    return metrics.authenticity


_READERS = {
    "settle_course_links": _read_by_the_delivery,
    "explanation_input": _read_by_the_explanation_input,
    "after_delivery": _read_after_delivery,
}


class TestFoundCourseLinks:
    def test_the_model_stays_strict_for_writing(self) -> None:
        with pytest.raises(ValidationError, match=UNKNOWN):
            FoundCourseLinks.model_validate(with_unknown(_found(), ()))

    @pytest.mark.parametrize("place", list(_FOUND_PLACES), ids=list(_FOUND_PLACES))
    @pytest.mark.parametrize("reader", list(_READERS), ids=list(_READERS))
    async def test_a_row_with_an_unknown_field_is_read(
        self, reader: str, place: str
    ) -> None:
        body = with_unknown(_found(), _FOUND_PLACES[place])

        with capture_logs() as logs:
            read = await _READERS[reader](body)

        if reader == "settle_course_links":
            assert read == {"c1": ()}
        elif reader == "explanation_input":
            assert read == FoundCourseLinks.model_validate(_found())
        else:
            # One criterion not met, and no link kept for it: authenticity 0.
            assert read == 0.0
        line = _the_line(logs)
        assert line["model"] == "FoundCourseLinks"
        assert line["fields"] == [".".join(map(str, (*_FOUND_PLACES[place], UNKNOWN)))]

    @pytest.mark.parametrize("reader", list(_READERS), ids=list(_READERS))
    async def test_a_row_with_another_defect_is_refused_as_before(
        self, reader: str
    ) -> None:
        body = with_unknown(_found(), ())
        del body["criteria"]

        with pytest.raises(ValidationError) as refused:
            await _READERS[reader](body)

        assert "missing" in [e["type"] for e in refused.value.errors()]


# ── PathCheckpoint and NodeSummaryRunState: Job.stage_progress ──────────────

_KEY = PathKey(AssignmentType.TASK, SubmissionState.FIRST)


_CHECKPOINT = PathCheckpoint.started(_KEY, ["safety", "verdicts"]).with_stage_done(
    "safety"
)


class TestPathCheckpoint:
    def test_the_model_stays_strict_for_writing(self) -> None:
        with pytest.raises(ValidationError, match=UNKNOWN):
            PathCheckpoint.model_validate(with_unknown(_CHECKPOINT.to_jsonb(), ()))

    def test_a_record_with_an_unknown_field_is_read(self) -> None:
        submission_id = uuid.uuid4()

        with capture_logs() as logs:
            read = PathCheckpoint.from_jsonb(
                with_unknown(_CHECKPOINT.to_jsonb(), ()), submission_id=submission_id
            )

        assert read.to_jsonb() == _CHECKPOINT.to_jsonb()
        line = _the_line(logs)
        assert line["model"] == "PathCheckpoint"
        assert line["fields"] == [UNKNOWN]
        assert line["submission_id"] == str(submission_id)

    def test_a_record_with_another_defect_is_refused_as_before(self) -> None:
        payload = with_unknown(_CHECKPOINT.to_jsonb(), ())
        payload["retries"] = "many"

        with pytest.raises(ValidationError, match="retries"):
            PathCheckpoint.from_jsonb(payload)

    async def test_the_checkpoint_of_a_revision_is_loaded(self) -> None:
        job = SimpleNamespace(
            id=uuid.uuid4(), stage_progress=with_unknown(_CHECKPOINT.to_jsonb(), ())
        )
        jobs = MagicMock()
        jobs.get_latest_for_subject = AsyncMock(return_value=job)

        with patch.object(path_checkpoint, "JobRepository", return_value=jobs):
            loaded = await path_checkpoint.load_checkpoint(MagicMock(), uuid.uuid4())

        assert loaded is not None
        assert loaded.to_jsonb() == _CHECKPOINT.to_jsonb()

    async def test_a_record_that_does_not_read_is_no_record_as_before(self) -> None:
        payload = with_unknown(_CHECKPOINT.to_jsonb(), ())
        payload["retries"] = "many"
        jobs = MagicMock()
        jobs.get_latest_for_subject = AsyncMock(
            return_value=SimpleNamespace(id=uuid.uuid4(), stage_progress=payload)
        )

        with patch.object(path_checkpoint, "JobRepository", return_value=jobs):
            loaded = await path_checkpoint.load_checkpoint(MagicMock(), uuid.uuid4())

        assert loaded is None

    def test_the_sweep_reads_the_checkpoint_of_a_job_the_same_way(self) -> None:
        job = SimpleNamespace(
            subject_id=uuid.uuid4(),
            stage_progress=with_unknown(_CHECKPOINT.to_jsonb(), ()),
        )
        broken = SimpleNamespace(
            subject_id=uuid.uuid4(), stage_progress={"retries": "many"}
        )

        read = _checkpoint_of(job)  # type: ignore[arg-type]
        assert read is not None
        assert read.to_jsonb() == _CHECKPOINT.to_jsonb()
        assert _checkpoint_of(broken) is None  # type: ignore[arg-type]


def _run_state() -> dict[str, Any]:
    return NodeSummaryRunState(vertex_node_id=uuid.uuid4()).to_jsonb()


class TestNodeSummaryRunState:
    @pytest.mark.parametrize("place", [(), ("scope",)], ids=["the root", "the scope"])
    def test_the_model_stays_strict_for_writing(
        self, place: tuple[str | int, ...]
    ) -> None:
        with pytest.raises(ValidationError, match=UNKNOWN):
            NodeSummaryRunState.model_validate(with_unknown(_run_state(), place))

    @pytest.mark.parametrize("place", [(), ("scope",)], ids=["the root", "the scope"])
    def test_a_record_with_an_unknown_field_is_read(
        self, place: tuple[str | int, ...]
    ) -> None:
        stored = _run_state()

        with capture_logs() as logs:
            read = NodeSummaryRunState.from_jsonb(with_unknown(stored, place))

        assert read.to_jsonb() == stored
        assert _the_line(logs)["fields"] == [".".join((*map(str, place), UNKNOWN))]

    def test_a_record_with_another_defect_is_refused_as_before(self) -> None:
        payload = with_unknown(_run_state(), ())
        payload["force"] = "perhaps"

        with pytest.raises(ValidationError, match="force"):
            NodeSummaryRunState.from_jsonb(payload)


# ── DraftBody and PublishedBody: the bodies of a test ───────────────────────


def _draft() -> DraftBody:
    return DraftBody(
        questions=(
            DraftQuestion(
                text="2 + 2?",
                options=(
                    DraftOption(text="4", correct=True),
                    DraftOption(text="5", correct=False),
                ),
            ),
        )
    )


_BODY_PLACES: dict[str, tuple[str | int, ...]] = {
    "the root": (),
    "a question": ("questions", 0),
    "an option": ("questions", 0, "options", 1),
}


class TestTheBodiesOfATest:
    @pytest.mark.parametrize("place", list(_BODY_PLACES), ids=list(_BODY_PLACES))
    def test_a_draft_with_an_unknown_field_is_read(self, place: str) -> None:
        stored = _draft().to_jsonb()

        with pytest.raises(ValidationError, match=UNKNOWN):
            DraftBody.model_validate(with_unknown(stored, _BODY_PLACES[place]))
        with capture_logs() as logs:
            read = DraftBody.from_jsonb(with_unknown(stored, _BODY_PLACES[place]))

        assert read == _draft()
        line = _the_line(logs)
        assert line["model"] == "DraftBody"
        assert line["fields"] == [".".join(map(str, (*_BODY_PLACES[place], UNKNOWN)))]

    @pytest.mark.parametrize("place", list(_BODY_PLACES), ids=list(_BODY_PLACES))
    def test_a_published_version_with_an_unknown_field_is_read(
        self, place: str
    ) -> None:
        published = published_form(_draft(), "eng")
        stored = published.to_jsonb()

        with pytest.raises(ValidationError, match=UNKNOWN):
            PublishedBody.model_validate(with_unknown(stored, _BODY_PLACES[place]))
        with capture_logs() as logs:
            read = PublishedBody.from_jsonb(with_unknown(stored, _BODY_PLACES[place]))

        assert read == published
        assert _the_line(logs)["model"] == "PublishedBody"

    def test_a_draft_with_another_defect_is_refused_as_before(self) -> None:
        stored = with_unknown(_draft().to_jsonb(), ("questions", 0))
        stored["questions"][0]["options"][0]["correct"] = "yes"

        with pytest.raises(ValidationError, match="correct"):
            DraftBody.from_jsonb(stored)

    def test_a_published_version_with_another_defect_is_refused_as_before(
        self,
    ) -> None:
        stored = with_unknown(published_form(_draft(), "eng").to_jsonb(), ())
        del stored["questions"]

        with pytest.raises(ValidationError, match="questions"):
            PublishedBody.from_jsonb(stored)


# ── The criteria of a task ──────────────────────────────────────────────────


def _document() -> list[dict[str, Any]]:
    return [
        {
            "id": "c1",
            "text": "Handles an empty list",
            "evidence": "returns [] for []",
            "weight": "must",
            "check_method": "mandatory_points",
            "soft_descent": False,
            "concepts": [],
            "mandatory_points": [{"id": "c1.p1", "text": "the empty case"}],
        },
        {
            "id": "c2",
            "text": "Has a docstring",
            "evidence": "a docstring under def",
            "weight": "should",
            "check_method": "model_verdict",
            "soft_descent": False,
            "concepts": [],
            "mandatory_points": [],
        },
    ]


def _noisy(*places: tuple[str | int, ...]) -> list[dict[str, Any]]:
    noisy = copy.deepcopy(_document())
    for place in places:
        node: Any = noisy
        for step in place:
            node = node[step]
        node[UNKNOWN] = MARKER
    return noisy


_CRITERIA_PLACES: dict[str, tuple[str | int, ...]] = {
    "a criterion": (1,),
    "a mandatory point": (0, "mandatory_points", 0),
}


class TestTheCriteriaOfATask:
    @pytest.mark.parametrize(
        "place", list(_CRITERIA_PLACES), ids=list(_CRITERIA_PLACES)
    )
    def test_the_models_stay_strict_for_writing(self, place: str) -> None:
        node: Any = _noisy(_CRITERIA_PLACES[place])
        for step in _CRITERIA_PLACES[place][:1]:
            node = node[step]
        model = Criterion if len(_CRITERIA_PLACES[place]) == 1 else MandatoryPoint
        for step in _CRITERIA_PLACES[place][1:]:
            node = node[step]

        with pytest.raises(ValidationError, match=UNKNOWN):
            model.model_validate(node)

    @pytest.mark.parametrize(
        "place", list(_CRITERIA_PLACES), ids=list(_CRITERIA_PLACES)
    )
    def test_a_list_with_an_unknown_field_is_read(self, place: str) -> None:
        submission_id = uuid.uuid4()

        with capture_logs() as logs:
            read = criteria_from_document(
                _noisy(_CRITERIA_PLACES[place]), submission_id=submission_id
            )

        assert read == criteria_from_document(_document())
        line = _the_line(logs)
        assert line["model"] == "criteria"
        assert line["fields"] == [
            ".".join(map(str, (*_CRITERIA_PLACES[place], UNKNOWN)))
        ]
        assert line["submission_id"] == str(submission_id)

    def test_the_authors_edit_is_read_the_same_way(self) -> None:
        with capture_logs():
            read = author_criteria_from_document(_noisy((0,), (1,)))

        assert [c.must_by_author for c in read] == [False, False]

    def test_another_defect_is_refused_as_before(self) -> None:
        noisy = _noisy((1,))
        noisy[1]["weight"] = "heavy"

        with pytest.raises(ValidationError, match="weight"):
            criteria_from_document(noisy)

    def test_a_rule_of_the_whole_list_still_holds_beside_an_unknown_field(
        self,
    ) -> None:
        """Ids repeat: a defect that shows once the unknown field is gone."""
        noisy = _noisy((1,))
        noisy[1]["id"] = "c1"
        noisy[1]["mandatory_points"] = [{"id": "c1.p1", "text": "x"}]
        noisy[1]["check_method"] = "mandatory_points"

        with pytest.raises(ValidationError, match="repeat"):
            criteria_from_document(noisy)

    def test_an_empty_list_is_still_no_list(self) -> None:
        with pytest.raises(ValidationError):
            criteria_from_document([])

    async def test_the_list_a_submission_was_judged_against_is_read(self) -> None:
        listed = SimpleNamespace(
            authored_document_id=uuid.uuid4(), criteria=_noisy((1,))
        )
        submission = SimpleNamespace(
            id=uuid.uuid4(), authored_document_id=listed.authored_document_id
        )
        session = MagicMock()
        session.get = AsyncMock(return_value=listed)

        with (
            patch.object(
                judged_list,
                "_judged_address",
                return_value=(CriteriaLayer.MODEL, uuid.uuid4()),
            ),
            capture_logs() as logs,
        ):
            read = await judged_list.read_judged_list(
                session,
                submission,  # type: ignore[arg-type]
                [],
            )

        assert read == criteria_from_document(_document())
        assert _the_line(logs)["submission_id"] == str(submission.id)


def test_the_helper_reads_a_stored_list_without_changing_it() -> None:
    """The data handed in is never touched: the pruning is on a copy."""
    noisy = _noisy((1,))
    before = copy.deepcopy(noisy)

    with capture_logs():
        criteria_from_document(noisy)

    assert noisy == before
