"""A submission about something else is not a security refusal (task 04).

Stage 2 stores an off-topic submission the same way it stores a prompt
injection: a rejection. The portal maps every rejection to "not opened", so the
student was told the work had not been checked and invited to send the same
foreign file again. It had been checked — that is how we know it is about
something else.

Two halves, and the second is the one that matters:

* the reason code ``off_topic``, given ONLY when off-topic is the whole of what
  the gate found — beside a real violation the sentence about a foreign file
  would make light of it;
* the state ``not_an_attempt``, which is what actually changes the sentence the
  student reads. Measured against today's portal build:

      state=not_an_attempt code=off_topic
        → "Надіслане не схоже на рішення цього завдання.
           Перевірте, що подаєте правильний файл."
      state=not_opened     code=off_topic
        → "Роботу не перевірено. Спробуйте надіслати ще раз."

  The phrase comes from the state family the portal already has, so it works
  before that build has ever heard of the new code.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from course_supporter.api.routes._portal_shared import (
    _PRESENTATION_STATE,
    OFF_TOPIC_REASON_CODE,
    curated_presentation,
    curated_rejection,
)
from course_supporter.security.schemas import ViolationCategory


def _rejected(violations: list[str] | None) -> MagicMock:
    """A submission Stage 2 refused, carrying the categories it found."""
    sub = MagicMock()
    sub.id = uuid.uuid4()
    sub.status = "rejected"
    sub.original_filename = "cv.pdf"
    safety: dict[str, Any] = {
        "source": "stage2",
        "is_safe": False,
        "reasoning": "INTERNAL classifier reasoning",
    }
    if violations is not None:
        safety["violations"] = violations
    sub.safety_result = safety
    return sub


class TestOffTopicAlone:
    def test_it_gets_its_own_code(self) -> None:
        rejection = curated_rejection(_rejected(["off_topic"]))

        assert rejection is not None
        assert rejection.code == "off_topic"

    def test_it_gets_the_state_that_changes_the_sentence(self) -> None:
        """Not ``not_opened``: it WAS opened, and read, and that is how we know."""
        presentation = curated_presentation(_rejected(["off_topic"]))

        assert presentation.state == "not_an_attempt"
        assert presentation.reason_code == "off_topic"

    def test_the_state_is_one_the_portal_has_a_phrase_for(self) -> None:
        """A state outside the family reaches the student as "Стан невідомий"."""
        assert "not_an_attempt" in set(_PRESENTATION_STATE.values())

    def test_the_code_is_the_categorys_own_value(self) -> None:
        """Not a second spelling of it: a rename in the taxonomy must not leave
        the portal keyed on a string nothing produces any more."""
        assert ViolationCategory.OFF_TOPIC.value == OFF_TOPIC_REASON_CODE


class TestOffTopicBesideSomethingElse:
    @pytest.mark.parametrize(
        "violations",
        [
            ["off_topic", "prompt_injection"],
            ["prompt_injection", "off_topic"],
            ["off_topic", "policy_violation", "suspicious_behavior"],
        ],
        ids=["injection-after", "injection-before", "three-of-them"],
    )
    def test_a_real_violation_beside_it_keeps_the_safety_phrase(
        self, violations: list[str]
    ) -> None:
        """Order does not matter and neither does count: the test is equality,
        not membership. "Your file looks like a CV" beside a prompt injection
        would make light of the injection."""
        rejection = curated_rejection(_rejected(violations))
        presentation = curated_presentation(_rejected(violations))

        assert rejection is not None
        assert rejection.code == "stage2_rejected"
        assert presentation.state == "not_opened"


class TestEverythingElseIsUnchanged:
    @pytest.mark.parametrize(
        "violations",
        [["prompt_injection"], ["policy_violation"], ["suspicious_behavior"], []],
        ids=["injection", "policy", "suspicious", "none-listed"],
    )
    def test_other_refusals_keep_the_code_and_the_state_they_had(
        self, violations: list[str]
    ) -> None:
        rejection = curated_rejection(_rejected(violations))
        presentation = curated_presentation(_rejected(violations))

        assert rejection is not None
        assert rejection.code == "stage2_rejected"
        assert presentation.state == "not_opened"

    def test_a_verdict_with_no_violations_key_is_not_off_topic(self) -> None:
        """Older rows, and the fixtures written before this existed."""
        presentation = curated_presentation(_rejected(None))

        assert presentation.reason_code == "stage2_rejected"
        assert presentation.state == "not_opened"

    def test_a_malformed_violations_value_is_not_off_topic(self) -> None:
        """A shape nobody writes today, refused rather than guessed at."""
        sub = _rejected(None)
        sub.safety_result["violations"] = "off_topic"

        assert curated_rejection(sub).code == "stage2_rejected"  # type: ignore[union-attr]

    def test_a_stage_1_refusal_is_untouched(self) -> None:
        """The doors answer in their own vocabulary and this does not reach them."""
        sub = _rejected(None)
        sub.safety_result = {"source": "stage1", "category": "bad_extension"}

        assert curated_rejection(sub).code == "bad_extension"  # type: ignore[union-attr]


class TestNoNewColumn:
    def test_the_category_is_read_off_the_stored_verdict(self) -> None:
        """DD-SP-Q: the answer is derived on read from what Stage 2 already
        wrote, not kept in a fourth place that would have to be kept in step.

        The proof is that changing only ``safety_result`` changes the answer —
        nothing else about the submission is touched between these two calls.
        """
        sub = _rejected(["prompt_injection"])
        assert curated_presentation(sub).state == "not_opened"

        sub.safety_result["violations"] = ["off_topic"]

        assert curated_presentation(sub).state == "not_an_attempt"
