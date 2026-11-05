"""The checked-in submission-path file (config/submission_paths.yaml)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from course_supporter.config import get_settings
from course_supporter.homework.path_config import (
    CodeStage,
    ModelStage,
    PathConfig,
    PathKey,
    ServedBy,
    SubmissionState,
    in_test_mode,
    load_path_config,
    model_stages,
    path_ceiling_estimate,
    validate_path_config,
)
from course_supporter.homework.path_stages import validate_stage_executors
from course_supporter.llm.ladder_config import load_ladder_config
from course_supporter.llm.registry import ModelRegistryConfig, load_registry
from course_supporter.models.source import AssignmentType

_PATHS_FILE = Path("config/submission_paths.yaml")
_REGISTRY_FILE = Path("config/external_services.yaml")

# Starting content of both stage definitions: (provider, model, pin). A change
# here is a conscious recalibration of the new path, not of today's Mentor.
# 2026-11-05: the deepseek-flash rung between the two left both copies at once
# (DeepSeek and Alibaba off the students' texts, the product owner's decision).
_COPIED_RUNGS = [
    ("mistral", "mistral-small-latest", None),
    ("gemini", "gemini-3.5-flash-lite", None),
]

# The providers the product owner took off the students' texts (2026-11-05):
# DeepSeek, both modes, and Alibaba's DashScope. No rung of the Mentor's two
# files may name one again.
_OFF_THE_STUDENTS_TEXTS = {"deepseek", "deepseek_thinking", "dashscope"}

# The text stages' ladders since 2026-11-05: (provider, model, pin), and the
# ceilings (tool_steps, money_usd, output_tokens) counted for them in the file.
_TEXT_STAGES = {
    "criteria_evaluation": (
        [
            ("gemini", "gemini-3.8-flash", 32768),
            ("mistral", "mistral-large-2512", 8192),
        ],
        (0, 0.38, 32768),
    ),
    "review_explanation": (
        [
            ("gemini", "gemini-3.8-flash", 32768),
            ("mistral", "mistral-small-latest", 8192),
        ],
        (0, 0.20, 32768),
    ),
}


@pytest.fixture(scope="module")
def config() -> PathConfig:
    return load_path_config(_PATHS_FILE)


@pytest.fixture(scope="module")
def registry() -> ModelRegistryConfig:
    return load_registry(_REGISTRY_FILE)


class TestCheckedInPaths:
    def test_file_passes_the_startup_checks(
        self, config: PathConfig, registry: ModelRegistryConfig
    ) -> None:
        # Exactly what the app and the worker run at boot: the shipped ladders
        # supply the names a path stage may not reuse, and the prompt files are
        # resolved from the repository root, as in production. Since task 07
        # this is also the proof that the switch of ``test`` passes the boot.
        validate_path_config(
            config,
            registry,
            ladder_stage_names=load_ladder_config(
                get_settings().ladders_dir
            ).stages.keys(),
        )
        validate_stage_executors(config.stages)

    def test_no_stage_name_repeats_a_ladder_stage(self, config: PathConfig) -> None:
        """Shared names would make the register sum two stages' costs as one."""
        ladder_names = set(load_ladder_config(get_settings().ladders_dir).stages)

        assert set(config.stages) & ladder_names == set()

    def test_every_stage_names_a_prompt_that_exists(self, config: PathConfig) -> None:
        for name, stage in model_stages(config).items():
            assert Path(stage.prompt_ref).is_file(), (
                f"stage {name!r} names a prompt that is not there: {stage.prompt_ref}"
            )

    def test_the_text_types_are_on_the_new_path_in_test_mode(
        self, config: PathConfig
    ) -> None:
        """Task 10, K5 (lock g): ``task`` and ``short_task`` move onto the new
        path in test mode — served by it, not charged for; ``test`` stays on
        the new path as it was, charged as usual (it costs nothing), and
        ``project`` stays with today's Mentor.

        Until task 07 every type stood on today's Mentor; task 07 moved
        ``test``. A type moves by an edit of these assertions, beside the
        file's own line — never as a side effect of some other change.
        """
        assert {t: d.served_by for t, d in config.task_types.items()} == {
            AssignmentType.TEST: ServedBy.NEW_PATH,
            AssignmentType.SHORT_TASK: ServedBy.NEW_PATH,
            AssignmentType.TASK: ServedBy.NEW_PATH,
            AssignmentType.PROJECT: ServedBy.TODAYS_MENTOR,
        }
        assert {t: in_test_mode(t, config) for t in AssignmentType} == {
            AssignmentType.TEST: False,
            AssignmentType.SHORT_TASK: True,
            AssignmentType.TASK: True,
            AssignmentType.PROJECT: False,
        }

    def test_every_type_describes_all_three_paths(self, config: PathConfig) -> None:
        """Twelve: four types by three states, ``short_task`` included (09b)."""
        described = [
            PathKey(t, s) for t, d in config.task_types.items() for s in d.paths
        ]
        assert len(described) == 12

    def test_test_paths_make_no_model_call(self, config: PathConfig) -> None:
        paths = config.task_types[AssignmentType.TEST].paths
        assert paths == {state: [] for state in SubmissionState}

    def test_a_project_has_the_classifier_on_its_first_submission_only(
        self, config: PathConfig
    ) -> None:
        paths = config.task_types[AssignmentType.PROJECT].paths
        assert paths[SubmissionState.FIRST] == ["safety", "attempt_classifier"]
        assert paths[SubmissionState.REPEAT_WITHOUT_REPLIES] == ["safety"]
        assert paths[SubmissionState.REPEAT_WITH_REPLIES] == ["safety"]

    @pytest.mark.parametrize(
        "task_type", [AssignmentType.TASK, AssignmentType.SHORT_TASK]
    )
    def test_a_text_task_is_judged_then_explained_on_every_path(
        self, config: PathConfig, task_type: AssignmentType
    ) -> None:
        """Task 09b: the verdicts, then their explanation, last on each path;
        the classifier on the first submission only. Task 10: the search for
        course links between the two, on every path."""
        paths = config.task_types[task_type].paths
        assert paths[SubmissionState.FIRST] == [
            "safety",
            "attempt_classifier",
            "criteria_evaluation",
            "course_link_lookup",
            "review_explanation",
        ]
        for state in (
            SubmissionState.REPEAT_WITHOUT_REPLIES,
            SubmissionState.REPEAT_WITH_REPLIES,
        ):
            assert paths[state] == [
                "safety",
                "criteria_evaluation",
                "course_link_lookup",
                "review_explanation",
            ]

    def test_the_course_link_lookup_is_deterministic_code(
        self, config: PathConfig
    ) -> None:
        """Task 10: no ladder, no prompt, no ceilings — and no money."""
        assert config.stages["course_link_lookup"] == CodeStage(
            code=True, deterministic=True
        )
        assert "course_link_lookup" not in model_stages(config)

    def test_the_explanation_reads_prompt_v2(self, config: PathConfig) -> None:
        """Task 10, K5: v2 carries the course links; v1 stays as history."""
        stage = config.stages["review_explanation"]
        assert isinstance(stage, ModelStage)
        assert stage.prompt_ref == "prompts/review_explanation/v2.md"
        assert stage.ceilings.money_usd == 0.20
        assert Path("prompts/review_explanation/v1.md").is_file()

    @pytest.mark.parametrize(
        ("task_type", "state", "estimate"),
        [
            (AssignmentType.TEST, SubmissionState.FIRST, 0.0),
            # 0.05 + 0.05 + 0.38 + 0 + 0.20: safety, the classifier, the
            # evaluation, the course links (code, free) and the explanation
            # (0.89 and 0.84 until the Gemini and Mistral ladders, 2026-11-05).
            (AssignmentType.TASK, SubmissionState.FIRST, 0.68),
            (AssignmentType.TASK, SubmissionState.REPEAT_WITH_REPLIES, 0.63),
            (AssignmentType.SHORT_TASK, SubmissionState.FIRST, 0.68),
            (AssignmentType.SHORT_TASK, SubmissionState.REPEAT_WITHOUT_REPLIES, 0.63),
            (AssignmentType.PROJECT, SubmissionState.FIRST, 0.10),
            (AssignmentType.PROJECT, SubmissionState.REPEAT_WITHOUT_REPLIES, 0.05),
        ],
    )
    def test_path_ceiling_estimates(
        self,
        config: PathConfig,
        task_type: AssignmentType,
        state: SubmissionState,
        estimate: float,
    ) -> None:
        key = PathKey(task_type, state)
        assert path_ceiling_estimate(config, key) == pytest.approx(estimate)

    @pytest.mark.parametrize("stage_name", ["safety", "attempt_classifier"])
    def test_stage_starts_from_the_copied_rungs(
        self, config: PathConfig, stage_name: str
    ) -> None:
        stage = config.stages[stage_name]
        assert isinstance(stage, ModelStage)
        rungs = [(r.provider, r.model, r.max_output_tokens) for r in stage.ladder]

        assert rungs == _COPIED_RUNGS
        assert all(r.reasoning is None for r in stage.ladder)
        assert stage.deterministic is True
        assert (
            stage.ceilings.tool_steps,
            stage.ceilings.money_usd,
            stage.ceilings.output_tokens,
        ) == (0, 0.05, 8192)

    @pytest.mark.parametrize("stage_name", sorted(_TEXT_STAGES))
    def test_a_text_stage_runs_gemini_then_mistral(
        self, config: PathConfig, stage_name: str
    ) -> None:
        """Gemini first (a strict schema on the wire), Mistral second (JSON mode).

        The Mistral rung is pinned at its registry cap: unpinned it would be
        sent the stage's 32768, which Gemini's reasoning needs and Mistral's
        registry entry does not allow.
        """
        stage = config.stages[stage_name]
        assert isinstance(stage, ModelStage)
        rungs, ceilings = _TEXT_STAGES[stage_name]

        assert [
            (r.provider, r.model, r.max_output_tokens) for r in stage.ladder
        ] == rungs
        assert all(r.reasoning is None for r in stage.ladder)
        assert (
            stage.ceilings.tool_steps,
            stage.ceilings.money_usd,
            stage.ceilings.output_tokens,
        ) == ceilings

    def test_no_rung_of_the_mentor_is_off_the_students_texts(
        self, config: PathConfig
    ) -> None:
        """Neither Mentor file names DeepSeek or Alibaba on any rung (2026-11-05)."""
        ladders = load_ladder_config(get_settings().ladders_dir)
        mentor = yaml.safe_load(
            (get_settings().ladders_dir / "ladders_mentor.yaml").read_text(
                encoding="utf-8"
            )
        )
        named = {
            (f"submission_paths.yaml:{name}", rung.provider)
            for name, stage in model_stages(config).items()
            for rung in stage.ladder
        } | {
            (f"ladders_mentor.yaml:{name}", rung.provider)
            for name in mentor["stages"]
            for rung in ladders.get_stage(name).ladder
        }

        assert {where for where, p in named if p in _OFF_THE_STUDENTS_TEXTS} == set()

    @pytest.mark.parametrize(
        ("stage_name", "tokens_in"),
        [
            # The largest realistic inputs of the files' money comments, at the
            # router's 3.5 characters a token.
            ("safety", 39_840),
            ("attempt_classifier", 38_147),
            ("criteria_evaluation", 56_021),
            ("review_explanation", 77_017),
        ],
    )
    def test_every_rung_stays_affordable_after_the_first_failed(
        self,
        config: PathConfig,
        registry: ModelRegistryConfig,
        stage_name: str,
        tokens_in: int,
    ) -> None:
        """The money comments' claim, at the registry's prices: rung 1 fits the
        ceiling, and what is left after it fits rung 2 — the router admits a
        rung by its estimate (input x price + pin x price). Money alone: the
        input budget is the router's other rule, and on review_explanation it
        skips the second rung at this input (77 017 > 0.5 x 131 072). The
        Gemini price revision turns this red for a stage whose number it must
        recount.
        """
        stage = config.stages[stage_name]
        assert isinstance(stage, ModelStage)
        attempts = []
        for rung in stage.ladder:
            pin = rung.max_output_tokens or stage.ceilings.output_tokens
            cost = registry.models[rung.model].estimate_cost(tokens_in, pin)
            assert cost is not None
            attempts.append(cost)
        first, second = attempts

        assert first <= stage.ceilings.money_usd
        assert second <= stage.ceilings.money_usd - first


class TestShortTaskPathsByConfigurationAlone:
    """Acceptance 5: a type's paths are added by editing the file, not the code."""

    def test_copy_with_short_task_paths_passes_the_startup_checks(
        self, tmp_path: Path, registry: ModelRegistryConfig
    ) -> None:
        raw = yaml.safe_load(_PATHS_FILE.read_text(encoding="utf-8"))
        raw["task_types"]["short_task"]["paths"] = {
            "first": ["safety", "attempt_classifier"],
            "repeat_without_replies": ["safety"],
            "repeat_with_replies": ["safety"],
        }
        copy_path = tmp_path / "submission_paths.yaml"
        copy_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

        edited = load_path_config(copy_path)
        validate_path_config(edited, registry)

        described = sum(len(d.paths) for d in edited.task_types.values())
        assert described == 12
        key = PathKey(AssignmentType.SHORT_TASK, SubmissionState.FIRST)
        assert path_ceiling_estimate(edited, key) == pytest.approx(0.10)
