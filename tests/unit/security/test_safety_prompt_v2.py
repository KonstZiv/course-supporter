"""The safety prompt v2: v1 plus one sentence, shipped on both roads (04.11).

The second line of defence behind ``DoorReading.safety_text``: should a
platform line about unopened files ever reach the safety check's input again,
the prompt says it is neither the student's content nor a violation. The locks:

* v1 is history and stays as it was, byte for byte;
* v2 is v1 with one paragraph of one sentence added, and nothing else changed;
* v2 renders from the same variables into the same data slots, under the same
  slot lock (the render itself: ``tests/unit/test_llm/test_prompt_slot_lock.py``);
* both roads -- today's Mentor's ladder and the new path's stage -- name v2.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from jinja2 import Environment, meta

from course_supporter.homework.path_config import ModelStage, load_path_config
from course_supporter.llm.ladder_config import load_ladder_config
from course_supporter.llm.prompt_loader_md import load_prompt, slot_names

_REPO = Path(__file__).resolve().parents[3]
_V1 = _REPO / "prompts" / "safety_check" / "v1.md"
_V2 = _REPO / "prompts" / "safety_check" / "v2.md"
_V2_REF = "prompts/safety_check/v2.md"

# v1 as merged before 04.11.
_V1_SHA256 = "9962a368c65f3bd493a69e9730592781379aae2c4001392d7fb04e45936c98bb"

_ADDED = (
    "\n"
    "Service lines the platform writes about files it did not open — a block "
    "headed\n"
    "`=== NOT OPENED (N) ===` whose every line is only a file name, a reason key\n"
    "such as `forbidden_type`, and a size — are, if they appear in the input,\n"
    "neither the student's content nor a violation: judge only the content of "
    "the\n"
    "files.\n"
)


def _variables(path: Path) -> set[str]:
    # Parsed for its variables, never rendered: nothing here to escape.
    env = Environment()  # noqa: S701
    return set(meta.find_undeclared_variables(env.parse(path.read_text())))


class TestV1IsHistory:
    def test_unchanged_byte_for_byte(self) -> None:
        assert hashlib.sha256(_V1.read_bytes()).hexdigest() == _V1_SHA256


class TestV2IsV1AndOneSentence:
    def test_the_one_difference_is_the_added_paragraph(self) -> None:
        v2 = _V2.read_text()

        assert v2.count(_ADDED) == 1
        assert v2.replace(_ADDED, "", 1) == _V1.read_text()

    def test_the_paragraph_is_one_sentence(self) -> None:
        sentence = " ".join(_ADDED.split())

        assert sentence.endswith(".")
        assert sentence.count(".") == 1

    def test_it_sits_in_the_system_section(self) -> None:
        prompt = load_prompt(_V2_REF, base_path=_REPO)

        assert prompt.system is not None
        assert "=== NOT OPENED (N) ===" in prompt.system


class TestTheSameSlots:
    def test_the_same_template_variables(self) -> None:
        assert _variables(_V2) == _variables(_V1)

    def test_the_same_data_slots(self) -> None:
        v1 = load_prompt("prompts/safety_check/v1.md", base_path=_REPO)
        v2 = load_prompt(_V2_REF, base_path=_REPO)

        assert slot_names(v2.system, v2.user, v2.assistant) == slot_names(
            v1.system, v1.user, v1.assistant
        )


class TestBothRoadsNameV2:
    def test_todays_mentor(self) -> None:
        stage = load_ladder_config(_REPO / "config").get_stage("safety_check")

        assert stage.prompt_ref == _V2_REF

    def test_the_new_path(self) -> None:
        stage = load_path_config(_REPO / "config" / "submission_paths.yaml").stages[
            "safety"
        ]

        assert isinstance(stage, ModelStage)
        assert stage.prompt_ref == _V2_REF
