"""Shadow price estimates (BIL-D): the arithmetic, the actions, the settings.

The estimates run against the repository's own configuration — the catalog,
the ladders, the paths and ``config/cost_estimate.yaml`` — so that a change of
any of them that moves an estimate shows up here. Two estimates are checked
against sums worked by hand (they are the examples of the pull request).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from course_supporter.cost_estimate.actions import (
    Catalog,
    MaterialFactors,
    estimate_processing,
    estimate_review,
    estimate_task_preparation,
    first_fitting_rung,
)
from course_supporter.cost_estimate.core import (
    ActionKind,
    EstimateError,
    expected_call_cost,
    fact_units,
    price_units,
    reserve_units,
    window_with_guard,
)
from course_supporter.cost_estimate.settings import (
    EstimateSettings,
    load_estimate_settings,
)
from course_supporter.cost_kinds import TariffWindow
from course_supporter.homework.path_config import (
    PathConfig,
    PathKey,
    SubmissionState,
    load_path_config,
    model_stages,
)
from course_supporter.llm.ladder_config import StageConfig, load_ladder_config
from course_supporter.llm.registry import ModelRegistryConfig, load_registry
from course_supporter.llm.token_budget import tokens_for_chars
from course_supporter.models.source import AssignmentType

CONFIG = Path("config")
MONDAY_NOON = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)  # off-peak everywhere


@pytest.fixture(scope="module")
def registry() -> ModelRegistryConfig:
    return load_registry(CONFIG / "external_services.yaml")


@pytest.fixture(scope="module")
def catalog(registry: ModelRegistryConfig) -> Catalog:
    return Catalog(
        models=registry.models,
        stt_chain=registry.get_chain("transcribe"),
        version=registry.catalog_version,
    )


@pytest.fixture(scope="module")
def settings() -> EstimateSettings:
    return load_estimate_settings(CONFIG / "cost_estimate.yaml")


@pytest.fixture(scope="module")
def paths() -> PathConfig:
    return load_path_config(CONFIG / "submission_paths.yaml")


@pytest.fixture(scope="module")
def ladders() -> dict[str, StageConfig]:
    return dict(load_ladder_config(CONFIG).stages)


def _review(
    paths: PathConfig,
    settings: EstimateSettings,
    catalog: Catalog,
    task_type: AssignmentType,
    state: SubmissionState,
    chars: int,
    at: datetime = MONDAY_NOON,
):
    # As the body hands it over (homework/path_runner.py): the model stages
    # of the path alone — a code stage calls no model and costs nothing.
    priced = model_stages(paths)
    stages = [s for s in paths.task_types[task_type].paths[state] if s in priced]
    return estimate_review(
        stage_names=stages,
        path_stages=priced,
        settings=settings,
        catalog=catalog,
        submission_chars=chars,
        at=at,
        inputs={"path_key": str(PathKey(task_type, state))},
    )


# ── Peak hours and the guard interval ──


class TestPeakWindow:
    """DeepSeek: peak 01:00-04:00 and 06:00-10:00 UTC, Monday to Friday."""

    @pytest.mark.parametrize(
        ("at", "guard", "window"),
        [
            (datetime(2026, 11, 2, 7, 0, tzinfo=UTC), 15, TariffWindow.PEAK),
            (datetime(2026, 11, 2, 0, 50, tzinfo=UTC), 15, TariffWindow.PEAK),
            (datetime(2026, 11, 2, 0, 50, tzinfo=UTC), 5, TariffWindow.OFF_PEAK),
            (datetime(2026, 11, 2, 0, 45, tzinfo=UTC), 15, TariffWindow.PEAK),
            (datetime(2026, 11, 2, 0, 44, tzinfo=UTC), 15, TariffWindow.OFF_PEAK),
            (datetime(2026, 11, 2, 4, 0, tzinfo=UTC), 15, TariffWindow.OFF_PEAK),
            (datetime(2026, 11, 2, 5, 50, tzinfo=UTC), 15, TariffWindow.PEAK),
            (datetime(2026, 11, 7, 7, 0, tzinfo=UTC), 15, TariffWindow.OFF_PEAK),
            # DD-BIL-HOL: Chinese public holidays
            (datetime(2027, 1, 1, 2, 0, tzinfo=UTC), 15, TariffWindow.OFF_PEAK),
            (datetime(2027, 1, 1, 0, 50, tzinfo=UTC), 15, TariffWindow.OFF_PEAK),
            (datetime(2027, 1, 8, 0, 50, tzinfo=UTC), 15, TariffWindow.PEAK),
        ],
    )
    def test_window(
        self,
        registry: ModelRegistryConfig,
        at: datetime,
        guard: int,
        window: TariffWindow,
    ) -> None:
        model = registry.models["deepseek-v4-pro"]
        assert window_with_guard(model, at, guard) is window

    def test_holiday_on_weekday_is_off_peak(
        self, registry: ModelRegistryConfig
    ) -> None:
        model = registry.models["deepseek-v4-pro"]
        at = datetime(2027, 1, 1, 2, 0, tzinfo=UTC)
        assert window_with_guard(model, at, guard_minutes=15) is TariffWindow.OFF_PEAK

    def test_holiday_morning_before_peak_is_off_peak(
        self, registry: ModelRegistryConfig
    ) -> None:
        model = registry.models["deepseek-v4-pro"]
        at = datetime(2027, 1, 1, 0, 50, tzinfo=UTC)
        assert window_with_guard(model, at, guard_minutes=15) is TariffWindow.OFF_PEAK

    def test_ordinary_weekday_before_peak_is_peak(
        self, registry: ModelRegistryConfig
    ) -> None:
        model = registry.models["deepseek-v4-pro"]
        at = datetime(2027, 1, 8, 0, 50, tzinfo=UTC)
        assert window_with_guard(model, at, guard_minutes=15) is TariffWindow.PEAK

    def test_a_model_without_tariff_is_flat(
        self, registry: ModelRegistryConfig
    ) -> None:
        model = registry.models["qwen3.7-max"]
        assert window_with_guard(model, MONDAY_NOON, 15) is TariffWindow.FLAT

    def test_peak_doubles_a_deepseek_step(
        self,
        settings: EstimateSettings,
        catalog: Catalog,
        ladders: dict[str, StageConfig],
    ) -> None:
        def at(moment: datetime) -> Decimal:
            return estimate_task_preparation(
                kind="criteria_decomposition",
                ladders=ladders,
                settings=settings,
                catalog=catalog,
                task_chars=7000,
                at=moment,
            ).e_usd

        off = at(MONDAY_NOON)
        peak = at(datetime(2026, 11, 2, 7, 0, tzinfo=UTC))
        assert peak == off * 2


# ── Units ──


class TestUnits:
    def test_price_rounds_up_once(self) -> None:
        e = Decimal("0.209656926")
        assert price_units(e, margin=Decimal("0.2"), units_per_usd=Decimal(100)) == 26
        assert (
            price_units(Decimal(1), margin=Decimal("0.2"), units_per_usd=Decimal(10))
            == 12
        )

    def test_reserve_is_twice_e_rounded_up(self) -> None:
        assert reserve_units(Decimal("0.209656926"), units_per_usd=Decimal(100)) == 42
        assert reserve_units(Decimal("0.5"), units_per_usd=Decimal(10)) == 10

    @pytest.mark.parametrize(
        ("usd", "units"),
        [
            (Decimal("0.0001"), 1),  # anything at all is at least one unit
            (Decimal("0.0149"), 1),
            (Decimal("0.015"), 2),  # half up
            (Decimal("0.0249"), 2),
            (Decimal("0.025"), 3),
            (Decimal(0), 0),
        ],
    )
    def test_fact_half_up_with_a_floor_of_one(self, usd: Decimal, units: int) -> None:
        assert fact_units(usd, units_per_usd=Decimal(100)) == units

    def test_the_provisional_rate(self, settings: EstimateSettings) -> None:
        """K = 1700 (BIL-CAL): the old path's median review, $0.058, is ≈ 100 units.

        ≈ 100 is E x K without the margin (98.6); by the model the same E is
        priced ceil(0.058 x 1.2 x 1700) = 119 and reserves ceil(2 x 0.058 x
        1700) = 198.
        """
        e = Decimal("0.058")
        k = settings.units_per_usd
        assert k is not None
        assert e * k == Decimal("98.6")
        assert price_units(e, margin=settings.margin, units_per_usd=k) == 119
        assert reserve_units(e, units_per_usd=k) == 198

    def test_no_rate_no_units(self) -> None:
        assert (
            price_units(Decimal(1), margin=Decimal("0.2"), units_per_usd=None) is None
        )
        assert reserve_units(Decimal(1), units_per_usd=None) is None
        assert fact_units(Decimal(1), units_per_usd=None) is None


# ── Review ──


class TestReview:
    def test_a_test_costs_nothing(
        self, paths: PathConfig, settings: EstimateSettings, catalog: Catalog
    ) -> None:
        estimate = _review(
            paths, settings, catalog, AssignmentType.TEST, SubmissionState.FIRST, 5000
        )
        assert estimate.kind is ActionKind.REVIEW
        assert estimate.e_usd == 0
        assert estimate.steps == ()

    def test_first_task_submission_by_hand(
        self, paths: PathConfig, settings: EstimateSettings, catalog: Catalog
    ) -> None:
        """10 000 characters -> 2 857 tokens of work beside each prompt.

        safety              mistral-small   5962 in, 65 out    0.000933300
        attempt_classifier  mistral-small   5270 in, 120 out   0.000862500
        criteria_evaluation qwen3.7-max    7727 in, 8548 out x 2  0.110141396
        review_explanation  qwen3.7-max   13256 in, 8192 out (cap) 0.062430992
        """
        estimate = _review(
            paths, settings, catalog, AssignmentType.TASK, SubmissionState.FIRST, 10_000
        )
        assert [
            (s.step, s.model, s.tokens_in, s.tokens_out) for s in estimate.steps
        ] == [
            ("safety", "mistral-small-latest", 5962, 65),
            ("attempt_classifier", "mistral-small-latest", 5270, 120),
            ("criteria_evaluation", "qwen3.7-max", 7727, 8548),
            ("review_explanation", "qwen3.7-max", 13256, 8192),
        ]
        assert estimate.e_usd == Decimal("0.174368188")
        assert not estimate.uncalibrated
        assert estimate.catalog_version == catalog.version
        assert estimate.inputs["path_key"] == "task/first"

    def test_criteria_evaluation_counts_two_requests(
        self, paths: PathConfig, settings: EstimateSettings, catalog: Catalog
    ) -> None:
        """Stage 2 calibration (READING-2026-11-07.md): criteria_evaluation
        runs two requests on live new-path task actions (first request + repeat
        about verdicts that cannot stand). The estimate prices two calls."""
        estimate = _review(
            paths, settings, catalog, AssignmentType.TASK, SubmissionState.FIRST, 10_000
        )
        [crit] = [s for s in estimate.steps if s.step == "criteria_evaluation"]
        assert crit.calls == Decimal(2)
        assert crit.price_in_per_1k_usd is not None
        assert crit.price_out_per_1k_usd is not None
        assert crit.tokens_in is not None
        assert crit.tokens_out is not None
        single_call_cost = (
            crit.tokens_in * crit.price_in_per_1k_usd
            + crit.tokens_out * crit.price_out_per_1k_usd
        ) / Decimal(1000)
        assert crit.cost_usd == single_call_cost * 2

    def test_a_repeat_skips_the_attempt_classifier(
        self, paths: PathConfig, settings: EstimateSettings, catalog: Catalog
    ) -> None:
        estimate = _review(
            paths,
            settings,
            catalog,
            AssignmentType.TASK,
            SubmissionState.REPEAT_WITHOUT_REPLIES,
            10_000,
        )
        assert [s.step for s in estimate.steps] == [
            "safety",
            "criteria_evaluation",
            "review_explanation",
        ]

    def test_every_stage_of_every_path_has_settings(
        self, paths: PathConfig, settings: EstimateSettings
    ) -> None:
        """Every model stage a path lists; a code stage has nothing to price."""
        used = {
            stage
            for declared in paths.task_types.values()
            for stages in declared.paths.values()
            for stage in stages
        }
        priced = set(model_stages(paths))
        assert used & priced <= set(settings.review)
        assert used - priced == {"course_link_lookup"}

    def test_the_course_link_lookup_is_not_priced(
        self, paths: PathConfig, settings: EstimateSettings, catalog: Catalog
    ) -> None:
        """Task 10: on the path, but no step of the estimate."""
        path = paths.task_types[AssignmentType.TASK].paths[SubmissionState.FIRST]
        assert "course_link_lookup" in path
        estimate = _review(
            paths, settings, catalog, AssignmentType.TASK, SubmissionState.FIRST, 10_000
        )
        assert "course_link_lookup" not in [s.step for s in estimate.steps]


class TestFirstFittingRung:
    def test_a_rung_too_small_for_the_input_is_skipped(
        self, paths: PathConfig, registry: ModelRegistryConfig
    ) -> None:
        """safety: 0.5 x 131 072 (mistral-small) = 65 536 tokens; the third rung
        (gemini-3.5-flash-lite) has a million."""
        stage = model_stages(paths)["safety"]
        assert first_fitting_rung("safety", stage, registry.models, 65_536).model == (
            "mistral-small-latest"
        )
        assert first_fitting_rung("safety", stage, registry.models, 100_000).model == (
            "deepseek-flash"
        )

    def test_no_rung_fits(
        self, paths: PathConfig, registry: ModelRegistryConfig
    ) -> None:
        with pytest.raises(EstimateError, match="no rung fits"):
            first_fitting_rung(
                "safety", model_stages(paths)["safety"], registry.models, 10_000_000
            )


# ── Processing ──


class TestProcessing:
    def test_ten_minutes_of_video_by_hand(
        self,
        settings: EstimateSettings,
        catalog: Catalog,
        ladders: dict[str, StageConfig],
    ) -> None:
        """600 s: 1102 words, 1879 tokens of speech, 28 998 of words JSON.

        transcribe            scribe_v1        600 s                0.037
        safety_check_authored mistral-small    3980 in, 8192 out    0.0055122
        video_pass_1_vision   qwen3.5-flash    6583 in, 8192 out x 19 0.048298209
        video_pass_2a_mapping deepseek-v4-pro 188529 in, 21000 out  0.16600914
        video_pass_2c_denoise gemini-3.1-flash-lite 3101/3500 x 0.5 0.003012625
        """
        estimate = estimate_processing(
            kind="video",
            factors=MaterialFactors(duration_sec=600.0),
            ladders=ladders,
            settings=settings,
            catalog=catalog,
            at=MONDAY_NOON,
        )
        by_step = {s.step: s for s in estimate.steps}
        assert by_step["transcribe"].billed_seconds == 600
        assert by_step["transcribe"].cost_usd == Decimal("0.037")
        assert by_step["video_pass_1_vision"].calls == 19
        assert by_step["video_pass_2a_mapping"].tokens_in == 188_529
        assert by_step["video_pass_2c_denoise"].calls == Decimal("0.5")
        assert estimate.e_usd == Decimal("0.259832174")
        assert estimate.kind is ActionKind.PROCESSING

    def test_peak_moves_only_deepseek(
        self,
        settings: EstimateSettings,
        catalog: Catalog,
        ladders: dict[str, StageConfig],
    ) -> None:
        estimate = estimate_processing(
            kind="video",
            factors=MaterialFactors(duration_sec=600.0),
            ladders=ladders,
            settings=settings,
            catalog=catalog,
            at=datetime(2026, 11, 2, 7, 0, tzinfo=UTC),
        )
        peak = {s.step for s in estimate.steps if s.peak}
        assert peak == {"video_pass_2a_mapping"}

    def test_audio(
        self,
        settings: EstimateSettings,
        catalog: Catalog,
        ladders: dict[str, StageConfig],
    ) -> None:
        estimate = estimate_processing(
            kind="audio",
            factors=MaterialFactors(duration_sec=3600.0),
            ladders=ladders,
            settings=settings,
            catalog=catalog,
            at=MONDAY_NOON,
        )
        steps = [s.step for s in estimate.steps]
        assert steps == [
            "transcribe",
            "safety_check_authored",
            "audio_pass_2a_mapping",
            "audio_pass_2c_denoise",
        ]
        # 60 min / 15 min a segment = 4 segments, half of them noisy.
        assert estimate.steps[-1].calls == 2

    def test_presentation_counts_the_second_safety_only_with_image_slides(
        self,
        settings: EstimateSettings,
        catalog: Catalog,
        ladders: dict[str, StageConfig],
    ) -> None:
        def steps(image_only: int) -> list[str]:
            return [
                s.step
                for s in estimate_processing(
                    kind="presentation",
                    factors=MaterialFactors(
                        slides=20,
                        image_only_slides=image_only,
                        text_chars=8000,
                        safety_chars=8000,
                    ),
                    ladders=ladders,
                    settings=settings,
                    catalog=catalog,
                    at=MONDAY_NOON,
                ).steps
            ]

        assert steps(0).count("safety_check_authored") == 1
        assert steps(3).count("safety_check_authored") == 2

    @pytest.mark.parametrize("kind", ["text", "web"])
    def test_text_and_web(
        self,
        kind: str,
        settings: EstimateSettings,
        catalog: Catalog,
        ladders: dict[str, StageConfig],
    ) -> None:
        estimate = estimate_processing(
            kind=kind,
            factors=MaterialFactors(text_chars=10_000, safety_chars=10_000),
            ladders=ladders,
            settings=settings,
            catalog=catalog,
            at=MONDAY_NOON,
        )
        assert [s.step for s in estimate.steps] == [
            "safety_check_authored",
            "pass_2a_mapping",
        ]
        assert estimate.steps[1].tokens_out == 1800

    def test_code(
        self,
        settings: EstimateSettings,
        catalog: Catalog,
        ladders: dict[str, StageConfig],
    ) -> None:
        estimate = estimate_processing(
            kind="code",
            factors=MaterialFactors(files=4, text_chars=20_000, safety_chars=20_000),
            ladders=ladders,
            settings=settings,
            catalog=catalog,
            at=MONDAY_NOON,
        )
        calls = {s.step: s.calls for s in estimate.steps}
        assert calls == {
            "safety_check_authored": 1,
            "code_skeleton_extraction": 2,
            "code_segment_description": 4,
            "code_summary": 1,
        }

    def test_an_unknown_factor_is_an_error(
        self,
        settings: EstimateSettings,
        catalog: Catalog,
        ladders: dict[str, StageConfig],
    ) -> None:
        with pytest.raises(EstimateError, match="duration_sec"):
            estimate_processing(
                kind="audio",
                factors=MaterialFactors(),
                ladders=ladders,
                settings=settings,
                catalog=catalog,
                at=MONDAY_NOON,
            )

    def test_every_processing_step_is_a_ladder_stage(
        self, settings: EstimateSettings, ladders: dict[str, StageConfig]
    ) -> None:
        assert set(settings.processing.steps) <= set(ladders)
        assert set(settings.task_preparation) <= set(ladders)


class TestTaskPreparation:
    @pytest.mark.parametrize("kind", ["criteria_decomposition", "key_explanation"])
    def test_one_call_of_the_stage(
        self,
        kind: str,
        settings: EstimateSettings,
        catalog: Catalog,
        ladders: dict[str, StageConfig],
    ) -> None:
        estimate = estimate_task_preparation(
            kind=kind,
            ladders=ladders,
            settings=settings,
            catalog=catalog,
            task_chars=7000,
            at=MONDAY_NOON,
        )
        assert estimate.kind is ActionKind.TASK_PREPARATION
        [step] = estimate.steps
        assert (step.step, step.model) == (kind, "deepseek-v4-pro")
        assert step.tokens_in == settings.task_preparation[kind].prompt_tokens + 2000


class TestExpectedCallCost:
    def test_prices_one_attempt_at_its_moment(
        self, registry: ModelRegistryConfig
    ) -> None:
        model = registry.models["deepseek-v4-pro"]
        off = expected_call_cost(
            model, at=MONDAY_NOON, guard_minutes=0, tokens_in=1000, tokens_out=1000
        )
        peak = expected_call_cost(
            model,
            at=datetime(2026, 11, 2, 7, 0, tzinfo=UTC),
            guard_minutes=0,
            tokens_in=1000,
            tokens_out=1000,
        )
        assert off == Decimal("0.00264")
        assert peak == off * 2

    def test_no_model_or_no_size_no_number(self, registry: ModelRegistryConfig) -> None:
        assert expected_call_cost(None, at=MONDAY_NOON, guard_minutes=0) is None
        model = registry.models["deepseek-v4-pro"]
        assert expected_call_cost(model, at=MONDAY_NOON, guard_minutes=0) is None


# ── Settings ──


class TestSettings:
    def test_the_file_loads_with_its_starting_values(
        self, settings: EstimateSettings
    ) -> None:
        assert settings.margin == Decimal("0.2")
        assert settings.units_per_usd == Decimal(1700)
        assert settings.guard_minutes == 15

    def test_calibration_flags(self, settings: EstimateSettings) -> None:
        """Stage 2 calibration (READING-2026-11-07.md): review stages and
        criteria_decomposition have calibrated=True. key_explanation and
        processing stages remain uncalibrated."""
        assert all(s.calibrated for s in settings.review.values())
        assert settings.task_preparation["criteria_decomposition"].calibrated
        assert not settings.task_preparation["key_explanation"].calibrated
        assert not any(s.calibrated for s in settings.processing.steps.values())

    @pytest.mark.parametrize(
        "change",
        [
            "margin: -0.1",
            "guard_minutes: -1",
            "units_per_usd: 0",
            "unknown_key: 1",
        ],
    )
    def test_a_bad_value_is_refused(self, tmp_path: Path, change: str) -> None:
        text = (CONFIG / "cost_estimate.yaml").read_text(encoding="utf-8")
        key = change.split(":")[0]
        lines = [ln for ln in text.splitlines() if not ln.startswith(f"{key}:")]
        bad = tmp_path / "bad.yaml"
        bad.write_text("\n".join([*lines, change]), encoding="utf-8")
        with pytest.raises(ValueError):
            load_estimate_settings(bad)

    def test_a_missing_file_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_estimate_settings(tmp_path / "absent.yaml")


# ── Live calibrated bounds (READING-2026-11-07.md) ──

# Fact costs from SQL queries in READING-2026-11-07.md:
# 01a1aa2e: 10 589 chars, task/first (READING-2026-11-07.md:152, 439)
FACT_TASK_01A1AA2E: Final = Decimal("0.100061")
# 01a1aa46: 12 811 chars, task/repeat_without_replies (READING-2026-11-07.md:155, 440)
FACT_TASK_01A1AA46: Final = Decimal("0.098725")
# 01a1aa80: 1 238 chars, task/repeat_without_replies (READING-2026-11-07.md:156)
FACT_TASK_01A1AA80: Final = Decimal("0.134982")
# 01a1ad2a: 12 838 chars, task/repeat_without_replies (READING-2026-11-07.md:157, 441)
FACT_TASK_01A1AD2A: Final = Decimal("0.109401")
# 01a1b354: 12 838 chars, task/repeat_without_replies (READING-2026-11-07.md:158, 442)
FACT_TASK_01A1B354: Final = Decimal("0.118365")
# criteria_decomposition in job 01a1aa2e (READING-2026-11-07.md:88, 352, 378)
FACT_TASK_PREP_01A1AA2E: Final = Decimal("0.025633")


class TestCalibratedLiveBounds:
    """Table test for the 5 live task review actions and 1 task_preparation action
    observed on 2026-11-07 (READING-2026-11-07.md).

    In all actions: fact < E (no underestimate), and fact / E is within target bounds.
    """

    @pytest.mark.parametrize(
        ("sub_id", "task_type", "state", "chars", "fact_usd"),
        [
            (
                "01a1aa2e",
                AssignmentType.TASK,
                SubmissionState.FIRST,
                10_589,
                FACT_TASK_01A1AA2E,
            ),
            (
                "01a1aa46",
                AssignmentType.TASK,
                SubmissionState.REPEAT_WITHOUT_REPLIES,
                12_811,
                FACT_TASK_01A1AA46,
            ),
            (
                "01a1aa80",
                AssignmentType.TASK,
                SubmissionState.REPEAT_WITHOUT_REPLIES,
                1_238,
                FACT_TASK_01A1AA80,
            ),
            (
                "01a1ad2a",
                AssignmentType.TASK,
                SubmissionState.REPEAT_WITHOUT_REPLIES,
                12_838,
                FACT_TASK_01A1AD2A,
            ),
            (
                "01a1b354",
                AssignmentType.TASK,
                SubmissionState.REPEAT_WITHOUT_REPLIES,
                12_838,
                FACT_TASK_01A1B354,
            ),
        ],
    )
    def test_task_review_actions_fact_over_e_bounds(
        self,
        paths: PathConfig,
        settings: EstimateSettings,
        catalog: Catalog,
        sub_id: str,
        task_type: AssignmentType,
        state: SubmissionState,
        chars: int,
        fact_usd: Decimal,
    ) -> None:
        estimate = _review(paths, settings, catalog, task_type, state, chars)
        assert estimate.e_usd > fact_usd, (
            f"action {sub_id}: fact {fact_usd} >= E {estimate.e_usd}"
        )
        ratio = fact_usd / estimate.e_usd
        assert Decimal("0.55") <= ratio <= Decimal("0.85"), (
            f"action {sub_id}: ratio {ratio} out of bounds"
        )

    def test_task_preparation_fact_over_e_bounds(
        self,
        settings: EstimateSettings,
        catalog: Catalog,
        ladders: dict[str, StageConfig],
    ) -> None:
        """criteria_decomposition in job 01a1aa2e (READING-2026-11-07.md:352)."""
        estimate = estimate_task_preparation(
            kind="criteria_decomposition",
            ladders=ladders,
            settings=settings,
            catalog=catalog,
            task_chars=3129,
            at=datetime(2026, 11, 5, 5, 28, tzinfo=UTC),
        )
        assert estimate.e_usd > FACT_TASK_PREP_01A1AA2E, (
            f"task_preparation: fact {FACT_TASK_PREP_01A1AA2E} >= E {estimate.e_usd}"
        )
        ratio = FACT_TASK_PREP_01A1AA2E / estimate.e_usd
        assert Decimal("0.75") <= ratio <= Decimal("0.85"), (
            f"task_preparation: ratio {ratio} out of bounds"
        )

    @pytest.mark.parametrize(
        ("stage_name", "chars", "unit_in"),
        [
            ("safety", 10_589, 5_509),
            ("attempt_classifier", 10_589, 4_955),
            ("criteria_evaluation", 10_589, 6_921),
            ("review_explanation", 1_238, 8_672),
        ],
    )
    def test_review_stage_prompt_plus_work_does_not_double_count(
        self,
        settings: EstimateSettings,
        stage_name: str,
        chars: int,
        unit_in: int,
    ) -> None:
        """Prompt tokens + work must not exceed observed unit_in * 1.25 + 1.

        Verifies that prompt_tokens was calibrated by subtracting work from
        observed unit_in, rather than taking max(unit_in) * 1.25 which would
        double count the submission text.
        """
        prompt_tokens = settings.review[stage_name].prompt_tokens
        work = tokens_for_chars(chars)
        est_in = prompt_tokens + work
        assert est_in <= unit_in * 1.25 + 1

    def test_task_preparation_prompt_plus_work_does_not_double_count(
        self,
        settings: EstimateSettings,
    ) -> None:
        """criteria_decomposition prompt + task work <= unit_in * 1.25 + 1."""
        prompt_tokens = settings.task_preparation[
            "criteria_decomposition"
        ].prompt_tokens
        work = 894  # 2602 - 1708
        unit_in = 11_148
        est_in = prompt_tokens + work
        assert est_in <= unit_in * 1.25 + 1
