"""The checked-in submission-path file (config/submission_paths.yaml)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import yaml

from course_supporter.config import get_settings
from course_supporter.homework.evaluator_version import evaluator_version
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
from course_supporter.homework.text_budget import path_context_budget_chars
from course_supporter.llm.ladder_config import load_ladder_config
from course_supporter.llm.registry import ModelRegistryConfig, load_registry
from course_supporter.models.source import AssignmentType

_PATHS_FILE = Path("config/submission_paths.yaml")
_REGISTRY_FILE = Path("config/external_services.yaml")

# Starting content of both stage definitions: (provider, model, pin). A change
# here is a conscious recalibration of the new path, not of today's Mentor.
_COPIED_RUNGS = [
    ("mistral", "mistral-small-latest", None),
    ("deepseek", "deepseek-flash", 8192),
    ("gemini", "gemini-3.5-flash-lite", None),
]


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

    def test_the_explanation_reads_prompt_v3(self, config: PathConfig) -> None:
        """2026-11-13: v3 drops the rule of the pass; v2 (task 10, K5: the
        course links) and v1 stay as history."""
        stage = config.stages["review_explanation"]
        assert isinstance(stage, ModelStage)
        assert stage.prompt_ref == "prompts/review_explanation/v3.md"
        assert stage.ceilings.money_usd == 0.24
        assert Path("prompts/review_explanation/v2.md").is_file()
        assert Path("prompts/review_explanation/v1.md").is_file()

    def test_the_explanations_money_covers_its_dearest_attempts(
        self, config: PathConfig, registry: ModelRegistryConfig
    ) -> None:
        """The arithmetic beside the stage, recounted for v3: the input v2's
        ceiling was counted for, and v3's prompt over v2's."""
        stage = config.stages["review_explanation"]
        assert isinstance(stage, ModelStage)
        tokens_in = _TEXT_EXPLANATION_TOKENS + _over(
            "prompts/review_explanation/v3.md", "prompts/review_explanation/v2.md"
        )
        qwen = _price(registry, "qwen3.7-max", tokens_in, 8_192)
        deepseek = _price(registry, "deepseek-flash", tokens_in, 8_192)
        reasoning = _price(registry, "qwen3.7-max", 0, _QWEN_REASONING_TOKENS)
        dearest = qwen + deepseek + reasoning

        assert tokens_in == 77_399
        assert dearest == pytest.approx(0.234302, abs=1e-6)
        assert dearest <= stage.ceilings.money_usd < dearest + 0.01
        # Both rungs stay reachable: the second fits in what the first leaves.
        assert stage.ceilings.money_usd - qwen >= deepseek

    @pytest.mark.parametrize(
        ("task_type", "state", "estimate"),
        [
            (AssignmentType.TEST, SubmissionState.FIRST, 0.0),
            # 0.05 + 0.05 + 0.55 + 0 + 0.24: safety, the classifier, the
            # evaluation, the course links (code, free) and the explanation.
            (AssignmentType.TASK, SubmissionState.FIRST, 0.89),
            (AssignmentType.TASK, SubmissionState.REPEAT_WITH_REPLIES, 0.84),
            (AssignmentType.SHORT_TASK, SubmissionState.FIRST, 0.89),
            (AssignmentType.SHORT_TASK, SubmissionState.REPEAT_WITHOUT_REPLIES, 0.84),
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


# Project chain K4: each project stage, its text twin, its prompt, its money.
_PROJECT_STAGES = {
    "project_criteria_evaluation": (
        "criteria_evaluation",
        "prompts/criteria_evaluation_project/v1.md",
        0.56,
    ),
    "project_review_explanation": (
        "review_explanation",
        "prompts/review_explanation_project/v1.md",
        0.24,
    ),
}
# What the twins' money ceilings were counted on (config/submission_paths.yaml):
# the router's characters a token, the text stages' largest inputs in tokens,
# and the reserve for qwen's reasoning on each of its requests.
_CHARS_PER_TOKEN = 3.5
_TEXT_EVALUATION_TOKENS = 56_021
_TEXT_EXPLANATION_TOKENS = 77_017
_QWEN_REASONING_TOKENS = 10_000
_CRITERIA = 37
_FLAG_IN_JSON = len(', "quote_from_base": true')


def _price(
    registry: ModelRegistryConfig, model: str, tokens_in: int, out: int
) -> float:
    cost = registry.models[model].cost_per_1k
    assert cost is not None, model
    return (tokens_in * cost.input + out * cost.output) / 1000


def _over(prompt: str, twin: str) -> int:
    """Tokens a project prompt adds to its twin's, at the router's estimate."""
    extra = len(Path(prompt).read_text(encoding="utf-8")) - len(
        Path(twin).read_text(encoding="utf-8")
    )
    return math.ceil(extra / _CHARS_PER_TOKEN)


class TestTheProjectsOwnStages:
    """Project chain K4: a project's evaluation and explanation, with their own
    prompts and ceilings, defined and runnable — and on no path yet."""

    @pytest.mark.parametrize("name", sorted(_PROJECT_STAGES))
    def test_a_stage_is_its_text_twin_but_for_the_prompt_and_the_money(
        self, config: PathConfig, name: str
    ) -> None:
        twin_name, prompt, money = _PROJECT_STAGES[name]
        stage, twin = config.stages[name], config.stages[twin_name]
        assert isinstance(stage, ModelStage)
        assert isinstance(twin, ModelStage)

        assert stage.prompt_ref == prompt
        assert stage.ceilings.money_usd == money
        own = {"prompt_ref": True, "ceilings": {"money_usd"}}
        assert stage.model_dump(exclude=own) == twin.model_dump(exclude=own)

    def test_no_path_of_any_type_lists_them(self, config: PathConfig) -> None:
        """Lock e: the switch of ``project`` is a later change, with its own
        live measurement; until then today's Mentor serves it."""
        listed = {
            name
            for declared in config.task_types.values()
            for stage_names in declared.paths.values()
            for name in stage_names
        }
        assert listed.isdisjoint(_PROJECT_STAGES)
        project = config.task_types[AssignmentType.PROJECT]
        assert project.served_by is ServedBy.TODAYS_MENTOR

    @pytest.mark.parametrize(
        ("broken", "message"),
        [
            ({"prompt_ref": "prompts/criteria_evaluation_project/v9.md"}, "prompt"),
            (
                {
                    "ladder": [
                        {
                            "provider": "dashscope",
                            "model": "qwen-unknown",
                            "reasoning": None,
                            "max_output_tokens": 16384,
                        }
                    ]
                },
                "qwen-unknown",
            ),
            ({"requires": ["vision"]}, "lacks required capabilities"),
        ],
    )
    def test_the_startup_check_reads_them_although_no_path_does(
        self,
        tmp_path: Path,
        registry: ModelRegistryConfig,
        broken: dict[str, object],
        message: str,
    ) -> None:
        """Lock a: the boot refuses a broken project stage, path or no path —
        its prompt, its ladder against the registry, its capabilities."""
        raw = yaml.safe_load(_PATHS_FILE.read_text(encoding="utf-8"))
        raw["stages"]["project_criteria_evaluation"].update(broken)
        copy_path = tmp_path / "submission_paths.yaml"
        copy_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

        with pytest.raises(ValueError, match="project_criteria_evaluation") as caught:
            validate_path_config(load_path_config(copy_path), registry)
        assert message in str(caught.value)

    def test_a_stage_without_its_executor_stops_the_boot(
        self, config: PathConfig
    ) -> None:
        stages = {**config.stages, "project_extra_stage": config.stages["safety"]}
        with pytest.raises(ValueError, match="'project_extra_stage'"):
            validate_stage_executors(stages)

    def test_the_input_they_were_counted_on_is_the_projects_budget(
        self, config: PathConfig, registry: ModelRegistryConfig
    ) -> None:
        """Both ceilings count the text stages' work of 131 072 characters: the
        new path's budget for ``project`` — with the two stages on its path
        or without them, the first rung of ``safety`` bounds it."""
        today = path_context_budget_chars(AssignmentType.PROJECT, config, registry)
        raw = config.model_dump(mode="json")
        raw["task_types"]["project"]["paths"]["first"] += [
            "project_criteria_evaluation",
            "course_link_lookup",
            "project_review_explanation",
        ]
        switched = path_context_budget_chars(
            AssignmentType.PROJECT, PathConfig.model_validate(raw), registry
        )

        assert today == switched == 131_072

    def test_the_evaluations_money_covers_its_dearest_attempts(
        self, config: PathConfig, registry: ModelRegistryConfig
    ) -> None:
        """The arithmetic in the file, executed: the first request on each
        rung, the repeat on qwen, qwen's reasoning on both its requests —
        rounded up to the cent, and no more."""
        stage = config.stages["project_criteria_evaluation"]
        assert isinstance(stage, ModelStage)
        tokens_in = _TEXT_EVALUATION_TOKENS + _over(
            "prompts/criteria_evaluation_project/v1.md",
            "prompts/criteria_evaluation/v1.md",
        )
        qwen = _price(registry, "qwen3.7-max", tokens_in, 16_384)
        deepseek = _price(registry, "deepseek-v4-pro", tokens_in, 32_768)
        reasoning = 2 * _price(registry, "qwen3.7-max", 0, _QWEN_REASONING_TOKENS)
        dearest = 2 * qwen + deepseek + reasoning

        assert tokens_in == 57_009
        assert dearest == pytest.approx(0.551891, abs=1e-6)
        assert dearest <= stage.ceilings.money_usd < dearest + 0.01

    def test_the_explanations_money_covers_its_dearest_attempts(
        self, config: PathConfig, registry: ModelRegistryConfig
    ) -> None:
        """As above: both rungs, qwen's reasoning, and the flag of the
        author's code on every criterion of the 37 not met."""
        stage = config.stages["project_review_explanation"]
        assert isinstance(stage, ModelStage)
        tokens_in = (
            _TEXT_EXPLANATION_TOKENS
            + _over(
                "prompts/review_explanation_project/v1.md",
                "prompts/review_explanation/v2.md",
            )
            + math.ceil(_CRITERIA * _FLAG_IN_JSON / _CHARS_PER_TOKEN)
        )
        qwen = _price(registry, "qwen3.7-max", tokens_in, 8_192)
        deepseek = _price(registry, "deepseek-flash", tokens_in, 8_192)
        reasoning = _price(registry, "qwen3.7-max", 0, _QWEN_REASONING_TOKENS)
        dearest = qwen + deepseek + reasoning

        assert tokens_in == 78_079
        assert dearest == pytest.approx(0.235526, abs=1e-6)
        assert dearest <= stage.ceilings.money_usd < dearest + 0.01
        # Both rungs stay reachable: the second fits in what the first leaves.
        assert stage.ceilings.money_usd - qwen >= deepseek

    @pytest.mark.parametrize(
        "task_type",
        [AssignmentType.TASK, AssignmentType.SHORT_TASK, AssignmentType.TEST],
    )
    def test_they_leave_the_evaluator_version_of_every_type_where_it_was(
        self, config: PathConfig, task_type: AssignmentType
    ) -> None:
        """A stage on no path is in no type's version: a review of unchanged
        work is still given again (task 13), as before chain K4."""
        raw = config.model_dump(mode="json")
        for name in _PROJECT_STAGES:
            del raw["stages"][name]
        without = PathConfig.model_validate(raw)

        assert evaluator_version(task_type, config) == evaluator_version(
            task_type, without
        )
