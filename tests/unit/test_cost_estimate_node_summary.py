"""Shadow estimate of a node-summary regeneration job (BIL-D, DD-BIL-NOEST).

Pure: the formulas against the repository's own catalog, ladders and settings,
and the two rules the estimate mirrors from the methodist (the full-text budget)
and from the path configuration (every model stage has a price line).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from course_supporter.agents.methodist import (
    FULL_TEXT_BUDGET_CHARS,
    OwnDocument,
    build_document_blocks,
)
from course_supporter.cost_estimate.actions import (
    Catalog,
    NodeSummaryFactors,
    estimate_node_summary_regeneration,
    estimate_review,
)
from course_supporter.cost_estimate.core import ActionKind, EstimateError
from course_supporter.cost_estimate.node_summary import (
    OwnDocumentSize,
    bottomup_chars,
)
from course_supporter.cost_estimate.settings import (
    EstimateSettings,
    load_estimate_settings,
)
from course_supporter.homework.path_config import (
    PathConfig,
    load_path_config,
    model_stages,
)
from course_supporter.llm.ladder_config import StageConfig, load_ladder_config
from course_supporter.llm.registry import ModelRegistryConfig, load_registry
from course_supporter.llm.token_budget import tokens_for_chars

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
def ladders() -> dict[str, StageConfig]:
    return dict(load_ladder_config(CONFIG).stages)


@pytest.fixture(scope="module")
def paths() -> PathConfig:
    return load_path_config(CONFIG / "submission_paths.yaml")


def _estimate(
    factors: NodeSummaryFactors,
    settings: EstimateSettings,
    catalog: Catalog,
    ladders: dict[str, StageConfig],
    at: datetime = MONDAY_NOON,
):
    return estimate_node_summary_regeneration(
        factors=factors,
        ladders=ladders,
        settings=settings,
        catalog=catalog,
        at=at,
    )


class TestRegenerationEstimate:
    def test_two_stale_nodes_three_topdown_by_hand(
        self,
        settings: EstimateSettings,
        catalog: Catalog,
        ladders: dict[str, StageConfig],
    ) -> None:
        """Bottom-up on deepseek-v4-pro, the first rung (1M window, ratio 0.5):
        off-peak $0.00066 in / $0.00198 out per 1k (config/external_services.yaml).

        node 1: 40 000 chars -> 11 428 tokens + 5 350 prompt = 16 778 in,
                32 768 out (the rung's pin)
                (16 778 x 0.00066 + 32 768 x 0.00198) / 1000 = 0.07595412
        node 2: 10 000 chars ->  2 857 tokens + 5 350 prompt =  8 207 in,
                32 768 out
                ( 8 207 x 0.00066 + 32 768 x 0.00198) / 1000 = 0.07029726
        top-down, three calls of 0 material + 4 110 prompt = 4 110 in,
                min(4 734, 8 192) out
                3 x (4 110 x 0.00066 + 4 734 x 0.00198) / 1000
                = 3 x 0.01208592 = 0.03625776
        """
        estimate = _estimate(
            NodeSummaryFactors(
                bottomup_chars=[40_000, 10_000], topdown_nodes=3, nodes=5
            ),
            settings,
            catalog,
            ladders,
        )
        assert estimate.kind is ActionKind.PROCESSING
        assert [
            (s.step, s.model, s.calls, s.tokens_in, s.tokens_out, s.cost_usd)
            for s in estimate.steps
        ] == [
            (
                "methodist_bottomup",
                "deepseek-v4-pro",
                Decimal(2),
                12_493,  # mean of 16 778 and 8 207, rounded up
                32_768,
                Decimal("0.14625138"),
            ),
            (
                "methodist_topdown",
                "deepseek-v4-pro",
                Decimal(3),
                4_110,
                4_734,
                Decimal("0.03625776"),
            ),
        ]
        assert estimate.e_usd == Decimal("0.18250914")
        assert estimate.uncalibrated  # every number is "не калібровано"
        assert estimate.catalog_version == catalog.version
        assert estimate.inputs["kind"] == "node_summary_regeneration"
        assert estimate.inputs["factors"] == {
            "nodes": 5,
            "bottomup_nodes": 2,
            "bottomup_chars": 50_000,
            "topdown_nodes": 3,
        }

    def test_a_fresh_course_costs_nothing(
        self,
        settings: EstimateSettings,
        catalog: Catalog,
        ladders: dict[str, StageConfig],
    ) -> None:
        estimate = _estimate(
            NodeSummaryFactors(bottomup_chars=[], topdown_nodes=0, nodes=7),
            settings,
            catalog,
            ladders,
        )
        assert estimate.steps == ()
        assert estimate.e_usd == 0

    def test_peak_doubles_it(
        self,
        settings: EstimateSettings,
        catalog: Catalog,
        ladders: dict[str, StageConfig],
    ) -> None:
        """Both rungs-1 are DeepSeek: the same job at 07:00 UTC costs twice."""
        factors = NodeSummaryFactors(bottomup_chars=[12_000], topdown_nodes=1, nodes=2)
        off = _estimate(factors, settings, catalog, ladders).e_usd
        peak = _estimate(
            factors,
            settings,
            catalog,
            ladders,
            at=datetime(2026, 11, 2, 7, 0, tzinfo=UTC),
        ).e_usd
        assert peak == off * 2

    def test_a_repeat_rate_in_the_settings_multiplies_the_calls(
        self,
        settings: EstimateSettings,
        catalog: Catalog,
        ladders: dict[str, StageConfig],
    ) -> None:
        steps = dict(settings.processing.steps)
        steps["methodist_bottomup"] = steps["methodist_bottomup"].model_copy(
            update={"calls": Decimal("1.5")}
        )
        repeated = settings.model_copy(
            update={
                "processing": settings.processing.model_copy(update={"steps": steps})
            }
        )
        factors = NodeSummaryFactors(bottomup_chars=[12_000], topdown_nodes=0, nodes=1)
        once = _estimate(factors, settings, catalog, ladders)
        again = _estimate(factors, repeated, catalog, ladders)
        assert again.steps[0].calls == Decimal("1.5")
        assert again.e_usd == once.e_usd * Decimal("1.5")

    def test_a_node_too_big_for_every_rung_fails_the_estimate_not_the_job(
        self,
        settings: EstimateSettings,
        catalog: Catalog,
        ladders: dict[str, StageConfig],
    ) -> None:
        """Past 0.5 x 1M tokens no rung fits; the hook swallows this error."""
        with pytest.raises(EstimateError, match="no rung fits"):
            _estimate(
                NodeSummaryFactors(
                    bottomup_chars=[2_000_000], topdown_nodes=0, nodes=1
                ),
                settings,
                catalog,
                ladders,
            )

    def test_the_stage_is_priced_on_the_first_rung_of_its_ladder(
        self, ladders: dict[str, StageConfig]
    ) -> None:
        assert ladders["methodist_bottomup"].ladder[0].model == "deepseek-v4-pro"
        assert ladders["methodist_topdown"].ladder[0].model == "deepseek-v4-pro"


# ── Pricing each node on its own rung, one line per model ──


@dataclass(frozen=True)
class _Rung:
    provider: str
    model: str
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class _Stage:
    ladder: tuple[_Rung, ...]
    input_budget_ratio: float | None = 0.5


def test_nodes_that_fit_different_rungs_get_a_line_each(
    settings: EstimateSettings, catalog: Catalog
) -> None:
    """mistral-small-latest (131 072 window, ratio 0.5 -> 65 536 tokens) first,
    deepseek-v4-pro behind it: a small node stays on the first rung, a node of
    300 000 characters (85 714 + 5 350 tokens) is skipped over to the second."""
    stage = _Stage(
        ladder=(
            _Rung("mistral", "mistral-small-latest", 8192),
            _Rung("deepseek_thinking", "deepseek-v4-pro", 32_768),
        )
    )
    estimate = estimate_node_summary_regeneration(
        factors=NodeSummaryFactors(
            bottomup_chars=[10_000, 300_000], topdown_nodes=0, nodes=2
        ),
        ladders={"methodist_bottomup": stage, "methodist_topdown": stage},
        settings=settings,
        catalog=catalog,
        at=MONDAY_NOON,
    )
    assert [(s.model, s.calls) for s in estimate.steps] == [
        ("mistral-small-latest", Decimal(1)),
        ("deepseek-v4-pro", Decimal(1)),
    ]


# ── The full-text budget is the methodist's ──


def _methodist_chars(docs: list[tuple[str, int]], children: int) -> int:
    """What ``build_document_blocks`` puts in front of the model, in characters."""
    own = [
        OwnDocument(
            title="т",
            description="о",
            main_concepts=["к"],
            secondary_concepts=[],
            content_char_count=len(text),
            material_role=role,
            task_type="task" if role == "task" else None,
            full_text=text,
        )
        for role, n in docs
        for text in ["x" * n]
    ]
    blocks = build_document_blocks(own)
    entries = [*blocks.methodological, *blocks.tasks, *blocks.educational]
    return (
        sum(len(e["full_text"] or "") for e in entries)
        + sum(len(e["title"]) + len(e["description"]) for e in entries)
        + sum(len(c) for e in entries for c in e["main_concepts"])
        + children
    )


def _sizes(docs: list[tuple[str, int]]) -> list[OwnDocumentSize]:
    return [
        OwnDocumentSize(
            entry_chars=3,  # a title, a description and a concept of one character
            text_chars=n if role in ("methodological", "task") else 0,
            methodological=role == "methodological",
            task=role == "task",
        )
        for role, n in docs
    ]


@pytest.mark.parametrize(
    "docs",
    [
        [],
        [("educational", 50_000)],
        [("methodological", 30_000), ("task", 20_000), ("educational", 90_000)],
        # the methodological text takes the budget first, the task goes as a summary
        [("task", 120_000), ("methodological", 150_000)],
        # a later, shorter task still fits after a longer one did not
        [("methodological", 150_000), ("task", 100_000), ("task", 40_000)],
        [("methodological", FULL_TEXT_BUDGET_CHARS), ("task", 1)],
        [("methodological", FULL_TEXT_BUDGET_CHARS + 1)],
    ],
)
def test_bottomup_chars_is_what_the_methodist_reads(
    docs: list[tuple[str, int]],
) -> None:
    assert bottomup_chars(_sizes(docs), children_chars=777) == _methodist_chars(
        docs, 777
    )


# ── Every model stage of the paths has a price line (the `project` path) ──


def test_every_model_stage_of_the_configuration_has_review_settings(
    paths: PathConfig, settings: EstimateSettings
) -> None:
    """Not only the stages a path lists today: a path switched to the new path
    later (`project`) lists stages that are already defined and must be priced."""
    assert set(model_stages(paths)) <= set(settings.review)


def test_the_project_path_after_its_switch_is_priced(
    paths: PathConfig, settings: EstimateSettings, catalog: Catalog
) -> None:
    """The stages `project` will run (task chain K4), 10 000 characters of work.

    project_criteria_evaluation: qwen3.7-max, 5 990 + 2 857 = 8 847 in,
        8 548 out, 2 calls
        (8 847 x 0.00165 + 8 548 x 0.004951) / 1000 x 2 = 0.113837396
    project_review_explanation: qwen3.7-max, 10 813 + 2 857 = 13 670 in,
        8 192 out (the rung's pin)
        (13 670 x 0.00165 + 8 192 x 0.004951) / 1000 = 0.063114092
    """
    priced = model_stages(paths)
    estimate = estimate_review(
        stage_names=["project_criteria_evaluation", "project_review_explanation"],
        path_stages=priced,
        settings=settings,
        catalog=catalog,
        submission_chars=10_000,
        at=MONDAY_NOON,
    )
    assert [
        (s.step, s.model, s.tokens_in, s.tokens_out, s.calls) for s in estimate.steps
    ] == [
        ("project_criteria_evaluation", "qwen3.7-max", 8_847, 8_548, Decimal(2)),
        ("project_review_explanation", "qwen3.7-max", 13_670, 8_192, Decimal(1)),
    ]
    assert estimate.steps[0].cost_usd == Decimal("0.113837396")
    assert estimate.steps[1].cost_usd == Decimal("0.063114092")
    assert estimate.uncalibrated  # no live data on this path yet


def test_the_prompt_sizes_in_the_settings_are_the_files(
    settings: EstimateSettings,
) -> None:
    """The measured part of each new line is the file's character count / 3.5."""
    bottomup = Path("prompts/methodist_bottomup/v3.md").read_text(encoding="utf-8")
    topdown = Path("prompts/methodist_topdown/v2.md").read_text(encoding="utf-8")
    assert settings.processing.steps["methodist_bottomup"].prompt_tokens == (
        tokens_for_chars(len(bottomup))
    )
    # the top-down line adds the node's own state: 8 000 characters, upper end
    assert settings.processing.steps["methodist_topdown"].prompt_tokens == (
        tokens_for_chars(len(topdown)) + tokens_for_chars(8_000)
    )
