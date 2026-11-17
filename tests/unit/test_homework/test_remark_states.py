"""The state of a remark between attempts (task 12a, K2): the pure rule.

The product owner's table of 2026-11-16, row for row, and the rule of
"credited" it stands on — the score's own (:func:`is_credited`): met, or
unconfirmed. No database: which attempt is the earlier one is pinned in
``tests/integration/test_submission_verdict_repository_db.py``, the review's
sections in ``test_text_result.py``, and the whole walk in
``tests/integration/test_text_review_e2e_db.py``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from course_supporter.criteria_kinds import (
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.homework import criteria_verdicts
from course_supporter.homework.criteria_verdicts import (
    credited_items,
    is_credited,
    remark_states,
    score_percent,
    with_remark_states,
)
from course_supporter.remark_kinds import RemarkState
from course_supporter.storage.submission_verdict_repository import VerdictRecord

MET, NOT_MET = VerdictValue.MET, VerdictValue.NOT_MET


def _met(item_id: str = "c3") -> VerdictRecord:
    return _row(item_id, MET, MET)


def _not_met(item_id: str = "c3") -> VerdictRecord:
    return _row(item_id, NOT_MET, NOT_MET)


def _unconfirmed(item_id: str = "c3") -> VerdictRecord:
    """The model said "met" and no line of the work bore it out."""
    return _row(item_id, NOT_MET, MET, quote_not_found=True)


def _row(
    item_id: str,
    verdict: VerdictValue,
    model_verdict: VerdictValue,
    *,
    quote_not_found: bool = False,
) -> VerdictRecord:
    point = "." in item_id
    return VerdictRecord(
        item_id,
        VerdictItemKind.POINT if point else VerdictItemKind.CRITERION,
        None if point else WeightCategory.SHOULD,
        verdict,
        model_verdict,
        quote_not_found=quote_not_found,
    )


def _state(earlier: dict[str, bool] | None, row: VerdictRecord) -> RemarkState | None:
    return remark_states(earlier, [row])[row.item_id]


class TestTheTableRowByRow:
    """Each row of the table, credited by the score's rule."""

    @pytest.mark.parametrize(
        "earlier",
        [None, {"c1": True}],
        ids=["nothing-to-compare-with", "the-item-is-only-in-this-attempt"],
    )
    def test_no_earlier_word_and_not_credited_is_new(
        self, earlier: dict[str, bool] | None
    ) -> None:
        """A first attempt, one at another version of the task (both read
        ``None``), or an earlier attempt without the item — an author added
        a criterion."""
        assert _state(earlier, _not_met()) is RemarkState.NEW

    @pytest.mark.parametrize(
        "earlier",
        [None, {"c1": False}],
        ids=["nothing-to-compare-with", "the-item-is-only-in-this-attempt"],
    )
    def test_no_earlier_word_and_credited_has_no_state(
        self, earlier: dict[str, bool] | None
    ) -> None:
        assert _state(earlier, _met()) is None

    def test_not_credited_then_and_now_is_open(self) -> None:
        assert _state({"c3": False}, _not_met()) is RemarkState.OPEN

    def test_not_credited_then_and_credited_now_is_fixed(self) -> None:
        assert _state({"c3": False}, _met()) is RemarkState.FIXED

    def test_credited_then_and_not_now_is_broken(self) -> None:
        assert _state({"c3": True}, _not_met()) is RemarkState.BROKEN

    def test_credited_then_and_now_has_no_state(self) -> None:
        assert _state({"c3": True}, _met()) is None


class TestTheItems:
    def test_an_item_gone_since_the_earlier_attempt_has_no_state(self) -> None:
        """An author removed the criterion: nothing now to say a state of —
        and the items that are still here read as before."""
        states = remark_states({"c3": False, "c9": False}, [_not_met("c3")])

        assert states == {"c3": RemarkState.OPEN}

    def test_every_row_has_its_state_criteria_and_points_alike(self) -> None:
        earlier = {"c1": True, "c2": False, "c2.p1": False, "c3": False}
        rows = [
            _met("c1"),
            _not_met("c2"),
            _met("c2.p1"),
            _not_met("c2.p2"),
            _met("c3"),
            _not_met("c4"),
        ]

        assert remark_states(earlier, rows) == {
            "c1": None,
            "c2": RemarkState.OPEN,
            "c2.p1": RemarkState.FIXED,
            "c2.p2": RemarkState.NEW,
            "c3": RemarkState.FIXED,
            "c4": RemarkState.NEW,
        }

    def test_nothing_to_compare_with_leaves_only_the_new(self) -> None:
        """A first attempt: what is not credited is new, the rest has none —
        the review lays out everything as before the states."""
        rows = [_met("c1"), _not_met("c2"), _unconfirmed("c3")]

        assert remark_states(None, rows) == {
            "c1": None,
            "c2": RemarkState.NEW,
            "c3": None,
        }


class TestCreditedIsTheScoresRule:
    """Not a second rule: "credited" is what the score counts as met."""

    @pytest.mark.parametrize(
        ("row", "credited"),
        [(_met(), True), (_not_met(), False), (_unconfirmed(), True)],
        ids=["met", "not-met", "unconfirmed"],
    )
    def test_the_score_counts_exactly_the_credited(
        self, row: VerdictRecord, credited: bool
    ) -> None:
        assert is_credited(row) is credited
        assert score_percent([row]) == (100 if credited else 0)

    def test_unconfirmed_then_and_not_met_now_is_broken(self) -> None:
        earlier = credited_items([_unconfirmed()])

        assert earlier == {"c3": True}
        assert _state(earlier, _not_met()) is RemarkState.BROKEN

    def test_not_met_then_and_unconfirmed_now_is_fixed(self) -> None:
        assert _state(credited_items([_not_met()]), _unconfirmed()) is (
            RemarkState.FIXED
        )

    def test_a_point_is_credited_on_the_same_terms(self) -> None:
        assert credited_items(
            [_met("c2.p1"), _not_met("c2.p2"), _unconfirmed("c2.p3")]
        ) == {"c2.p1": True, "c2.p2": False, "c2.p3": True}


class TestTheRecordsToStore:
    """What the evaluation stage writes: each record with its state."""

    def test_each_record_gets_its_items_state_and_nothing_else_moves(self) -> None:
        settled = (_met("c1"), _not_met("c2"), _met("c2.p1"), _not_met("c3"))
        earlier = [_not_met("c1"), _not_met("c2"), _met("c2.p1"), _met("c3")]

        stored = with_remark_states(settled, earlier)

        assert [record.remark_state for record in stored] == [
            RemarkState.FIXED,
            RemarkState.OPEN,
            None,
            RemarkState.BROKEN,
        ]
        assert [replace(record, remark_state=None) for record in stored] == list(
            settled
        )

    def test_nothing_to_compare_with_writes_only_new(self) -> None:
        stored = with_remark_states((_met("c1"), _not_met("c2")), None)

        assert [record.remark_state for record in stored] == [None, RemarkState.NEW]

    def test_the_same_records_and_rows_give_the_same_states(self) -> None:
        """A stage run again writes its states again as they were."""
        settled = (_met("c1"), _not_met("c2"))
        earlier = [_not_met("c1"), _met("c2")]

        assert with_remark_states(settled, earlier) == with_remark_states(
            settled, earlier
        )


def test_the_states_are_the_four_words_of_the_table() -> None:
    """The words a stored state is written in (``remark_kinds``, the column's
    CHECK): the table's four, no other."""
    assert [state.value for state in RemarkState] == [
        "new",
        "open",
        "fixed",
        "broken",
    ]


def test_the_verdict_rules_were_raised_for_the_states() -> None:
    """The states change what a review says: the version moved 6 -> 7, so a
    review written before them is never given again as a copy."""
    assert criteria_verdicts.VERDICT_RULES_VERSION >= 7
