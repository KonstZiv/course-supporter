"""The key and the lifetime of the evaluation stage's kept first answer (DD-SP-DG).

No network and no Redis: the digest is a pure function of what the model is
shown and of the stage, and it must be the same in every process — a retry
runs in another worker, long after the pass that wrote the key.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from course_supporter.agents.criteria_evaluator import EvaluationInput
from course_supporter.config import Settings
from course_supporter.criteria_kinds import VerdictValue
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.earlier_review import AttemptMemory, PreviousVerdict
from course_supporter.homework.first_answer_store import (
    first_answer_digest,
    first_answer_key,
    first_answer_ttl_s,
)
from course_supporter.homework.path_config import ModelStage, load_path_config
from course_supporter.homework.path_stages import CRITERIA_EVALUATION
from course_supporter.homework.student_replies import EvaluationReply
from course_supporter.llm.stage_router import StageExecution
from course_supporter.remark_kinds import CommentKind

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SUBMISSION = uuid.UUID("01999999-0000-7000-8000-000000000001")

# A fresh interpreter loads this very file by its path (``test_homework`` is no
# package) and writes the digest of the fixture.
_PROGRAM = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("fixture", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.stdout.write(module.digest_of_the_fixture())
"""


def _criterion(cid: str, text: str = "Handles an empty list") -> Criterion:
    return Criterion.model_validate(
        {
            "id": cid,
            "text": text,
            "evidence": f"What shows {cid}",
            "weight": "must",
            "check_method": "mandatory_points",
            "soft_descent": False,
            "concepts": ["loops"],
            "mandatory_points": [{"id": f"{cid}.p1", "text": "Point 1"}],
        }
    )


def _shown() -> EvaluationInput:
    return EvaluationInput(
        task_title="Fibonacci",
        task_description="Write it",
        task_text="Text of the task — з українськими літерами",
        criteria=(_criterion("c1"), _criterion("c2")),
        submission_text="def fibonacci(n):\n    return n\n",
        language="Ukrainian",
        replies=(EvaluationReply("c1", CommentKind.OBJECTION, "I disagree"),),
        memory=AttemptMemory(
            earlier_submission_id=_SUBMISSION,
            verdicts=(PreviousVerdict("c1", VerdictValue.MET, "def f", None, False),),
            diff="-a\n+b\n",
            criteria_changed=False,
            task_changed=False,
            rewritten=False,
            earlier_work_unavailable=False,
            changed_share=0.1,
            lines_added=1,
            lines_removed=1,
        ),
    )


def _execution() -> StageExecution:
    from course_supporter.homework.path_stages import router_stage_config

    config = load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")
    stage = config.stages[CRITERIA_EVALUATION]
    assert isinstance(stage, ModelStage)
    return StageExecution(
        stage=router_stage_config(stage), stage_name=CRITERIA_EVALUATION
    )


def digest_of_the_fixture() -> str:
    """What a fresh process computes — the subprocess test runs this."""
    return first_answer_digest(_shown(), _execution())


class TestTheDigest:
    def test_the_same_input_gives_the_same_digest_in_two_processes(self) -> None:
        here = digest_of_the_fixture()

        def there(seed: str) -> str:
            done = subprocess.run(  # noqa: S603
                [sys.executable, "-I", "-c", _PROGRAM, __file__],
                cwd=_REPO_ROOT,
                env={
                    "PYTHONHASHSEED": seed,
                    "PYTHONPATH": str(_REPO_ROOT),
                    "ENVIRONMENT": "testing",
                    "PATH": "",
                },
                capture_output=True,
                text=True,
                check=True,
            )
            return done.stdout

        assert there("1") == here
        assert there("2") == here
        assert len(here) == 64

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param(
                lambda s: replace(s, criteria=(*s.criteria, _criterion("c3"))),
                id="another-criteria-list",
            ),
            pytest.param(
                lambda s: replace(
                    s, criteria=(_criterion("c1", "Another text"), s.criteria[1])
                ),
                id="a-criterion-edited",
            ),
            pytest.param(
                lambda s: replace(
                    s,
                    replies=(
                        *(s.replies or ()),
                        EvaluationReply("c2", CommentKind.OBJECTION, "new reply"),
                    ),
                ),
                id="a-new-reply",
            ),
            pytest.param(lambda s: replace(s, replies=None), id="no-replies"),
            pytest.param(lambda s: replace(s, memory=None), id="no-memory"),
            pytest.param(
                lambda s: replace(s, submission_text=s.submission_text + "#"),
                id="another-work",
            ),
            pytest.param(lambda s: replace(s, language="English"), id="language"),
        ],
    )
    def test_another_input_gives_another_digest(self, change: object) -> None:
        shown = _shown()
        assert callable(change)

        assert first_answer_digest(change(shown), _execution()) != (
            first_answer_digest(shown, _execution())
        )

    def test_another_stage_gives_another_digest(self) -> None:
        execution = _execution()
        ladder = execution.stage.ladder[:1]
        other = replace(
            execution, stage=execution.stage.model_copy(update={"ladder": ladder})
        )
        other_prompt = replace(
            execution,
            stage=execution.stage.model_copy(
                update={"prompt_ref": "prompts/criteria_evaluation/other.md"}
            ),
        )

        base = first_answer_digest(_shown(), execution)
        assert first_answer_digest(_shown(), other) != base
        assert first_answer_digest(_shown(), other_prompt) != base


class TestTheKey:
    def test_the_key_names_the_submission_the_stage_and_the_digest(self) -> None:
        shown, execution = _shown(), _execution()

        key = first_answer_key(_SUBMISSION, CRITERIA_EVALUATION, shown, execution)

        assert key == (
            f"cs:first_answer:{_SUBMISSION}:{CRITERIA_EVALUATION}:"
            f"{first_answer_digest(shown, execution)}"
        )

    def test_another_stage_or_submission_is_another_key(self) -> None:
        shown, execution = _shown(), _execution()
        base = first_answer_key(_SUBMISSION, CRITERIA_EVALUATION, shown, execution)

        assert base != first_answer_key(
            _SUBMISSION, "project_criteria_evaluation", shown, execution
        )
        assert base != first_answer_key(
            uuid.UUID(int=2), CRITERIA_EVALUATION, shown, execution
        )


class TestTheLifetime:
    def test_the_lifetime_is_every_pass_with_its_deferral_and_half_an_hour(
        self,
    ) -> None:
        settings = Settings(
            submission_path_max_retries=2, submission_path_retry_defer_s=60
        )

        assert first_answer_ttl_s(settings) == (2 + 1) * 60 + 1800 == 1980

    def test_it_follows_the_settings(self) -> None:
        settings = Settings(
            submission_path_max_retries=4, submission_path_retry_defer_s=30
        )

        assert first_answer_ttl_s(settings) == 5 * 30 + 1800

    def test_the_shipped_settings_give_1980_seconds(self) -> None:
        assert first_answer_ttl_s(Settings()) == 1980
