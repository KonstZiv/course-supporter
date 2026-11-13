"""Locks: the ``deterministic`` flag of a stage acts (2026-11-13).

The product owner's decision of 2026-11-13: a ``deterministic: true`` stage
sends temperature 0 explicitly and a fixed reproducibility seed to every
provider whose own documentation names one (DashScope ``seed``, Mistral
``random_seed``; DeepSeek documents none); a ``deterministic: false`` stage
sends byte for byte what it sent before. Bodies are captured at the transport,
through the real router and the production connector classes
(``_wire_capture.py``).

Locks (lettered a-d here in the order of the brief; the fifth, the
measurement script, is ``tests/unit/test_measure_determinism.py``):

* (a) deterministic + a provider that takes a seed -> the seed (the one
  constant) and temperature 0 in the HTTP body;
* (b) deterministic + a provider that takes none -> no seed, the body is what
  it was on ``main``, and the call goes through;
* (c) not deterministic -> the body is the one fingerprinted on ``main``;
* (d) the trace -> a ``stage_router_reproducibility`` line, joined to the
  register row by ``input_hash``, for (a); no line for (c).

Beside them: the set of connectors that take a seed is the documented one; a
seed that reaches a connector which cannot send it is refused before the call;
the path stages carry the flag from ``config/submission_paths.yaml``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

from course_supporter.homework.path_config import load_path_config, model_stages
from course_supporter.homework.path_stages import router_stage_config
from course_supporter.llm.ladder_config import LadderConfig
from course_supporter.llm.providers import PROVIDER_REGISTRY
from course_supporter.llm.providers.base import RequestConfigError
from course_supporter.llm.reproducibility import (
    DETERMINISTIC_TEMPERATURE,
    REPRODUCIBILITY_SEED,
)
from course_supporter.llm.schemas import LLMRequest
from course_supporter.llm.stage_router import StageRouter

from ._wire_capture import (
    PROMPT,
    RUNGS,
    STAGE_NAME,
    connector,
    fingerprint,
    registry,
    run_and_capture,
    stage,
)

_SUBMISSION_PATHS = Path("config/submission_paths.yaml")

# Taken on main at f797553 — before the flag reached the router — by
# ``run_and_capture`` over each provider with a stage that carries no flag:
# (provider, with a response schema) -> fingerprint of the request bodies.
_ON_MAIN: dict[tuple[str, bool], str] = {
    ("mistral", False): (
        "0367c38552eddcb635a093ea0a74f205d79316d1b031e68623eca2098902c6bc"
    ),
    ("mistral", True): (
        "500b80b0ba094f019832b38e998b0465ccb82b258b1b5df9472d65a0a766ac7c"
    ),
    ("deepseek", False): (
        "cf3a3ed8ad8f7d86991a77612f1090627b8150b42cedbe3559946c236f768a3f"
    ),
    ("deepseek", True): (
        "216959c1e1b6a3bd60c2de10ad03e24f00d2b3b78290a4f2b52af385a61cd0a7"
    ),
    ("deepseek_thinking", False): (
        "9b16c7e8e49119748c7c9fdbde89c4ca60006b9d5625699296d5b98e37ded51d"
    ),
    ("deepseek_thinking", True): (
        "309809c4dc2f6de4bd7fe4e076abc3d4b3ab50c8420a84f2be8e65a285f2b241"
    ),
    ("dashscope", False): (
        "5058bf30e134c5c07df132e341297e23e8cafb8a5a199d06afc26b02d788e5d1"
    ),
    ("dashscope", True): (
        "befe5a89f6a723e53a3033b5074da3456a6b157e4fa61be93c790b5dce5713f3"
    ),
    ("gemini", False): (
        "e5066653c9a2c2531cf1225449b45ac0798d5de80d768d15be0dedb4a322dda4"
    ),
    ("gemini", True): (
        "ea75941b0d95d3f144ceefdba38cc96d80e1c61d3f1657f27e57150f79772f44"
    ),
}

# Where each seed-taking vendor carries the two values in its body.
_TAKES_A_SEED = ["mistral", "dashscope"]
_TAKES_NO_SEED = ["deepseek", "deepseek_thinking", "gemini"]


def _seed_and_temperature(provider: str, body: dict[str, Any]) -> tuple[Any, Any]:
    if provider == "dashscope":
        parameters = body["parameters"]
        return parameters.get("seed"), parameters.get("temperature")
    return body.get("random_seed"), body.get("temperature")


@pytest.fixture
def register(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    async def _fake_persist(_session_factory: Any, **kwargs: Any) -> None:
        rows.append(kwargs)

    monkeypatch.setattr("course_supporter.llm.stage_router._persist", _fake_persist)
    return rows


class TestTheConstants:
    def test_the_seed_is_in_every_documented_range(self) -> None:
        # DashScope: [0, 2**31 - 1]; Mistral: an integer, no range stated.
        assert 0 <= REPRODUCIBILITY_SEED <= 2**31 - 1
        assert DETERMINISTIC_TEMPERATURE == 0.0


class TestSeedOnTheWire:
    """(a) A provider that takes a seed gets it, with temperature 0."""

    @pytest.mark.parametrize("with_schema", [False, True], ids=["json", "schema"])
    @pytest.mark.parametrize("provider", _TAKES_A_SEED)
    async def test_seed_and_temperature_in_the_body(
        self, provider: str, with_schema: bool
    ) -> None:
        bodies, _ = await run_and_capture(
            provider, stage(provider, deterministic=True), with_schema=with_schema
        )

        assert len(bodies) == 1
        body = json.loads(bodies[0])
        assert _seed_and_temperature(provider, body) == (REPRODUCIBILITY_SEED, 0.0)

    async def test_mistral_gets_its_own_name_only(self) -> None:
        bodies, _ = await run_and_capture(
            "mistral", stage("mistral", deterministic=True), with_schema=False
        )
        body = json.loads(bodies[0])
        assert "seed" not in body
        assert body["random_seed"] == REPRODUCIBILITY_SEED

    @pytest.mark.parametrize("provider", list(RUNGS))
    async def test_temperature_is_set_not_inherited(self, provider: str) -> None:
        # Temperature 0 was already on the wire as LLMRequest's default; the
        # stage now sets it itself, so a change of that default cannot reach
        # a deterministic stage. A stage without the flag keeps the default.
        router = StageRouter(LadderConfig(), {}, registry=registry())
        entry = stage(provider).ladder[0]
        flagged = router._build_request(
            PROMPT, entry, STAGE_NAME, deterministic=True, provider=connector(provider)
        )
        plain = router._build_request(PROMPT, entry, STAGE_NAME)

        assert "temperature" in flagged.model_fields_set
        assert flagged.temperature == DETERMINISTIC_TEMPERATURE
        assert "temperature" not in plain.model_fields_set


class TestNoSeedWhereNoneIsDocumented:
    """(b) A provider that takes no seed: nothing added, nothing fails."""

    @pytest.mark.parametrize("with_schema", [False, True], ids=["json", "schema"])
    @pytest.mark.parametrize("provider", _TAKES_NO_SEED)
    async def test_body_is_todays_and_the_call_goes_through(
        self, provider: str, with_schema: bool
    ) -> None:
        bodies, _ = await run_and_capture(
            provider, stage(provider, deterministic=True), with_schema=with_schema
        )

        assert len(bodies) == 1
        assert b"seed" not in bodies[0]
        assert fingerprint(bodies) == _ON_MAIN[(provider, with_schema)]


class TestNotDeterministicIsUnchanged:
    """(c) Without the flag the body is byte for byte the one on ``main``."""

    @pytest.mark.parametrize("with_schema", [False, True], ids=["json", "schema"])
    @pytest.mark.parametrize("provider", list(RUNGS))
    @pytest.mark.parametrize(
        "flags", [{}, {"deterministic": False}], ids=["absent", "false"]
    )
    async def test_body_matches_main(
        self, provider: str, with_schema: bool, flags: dict[str, Any]
    ) -> None:
        bodies, _ = await run_and_capture(
            provider, stage(provider, **flags), with_schema=with_schema
        )

        assert fingerprint(bodies) == _ON_MAIN[(provider, with_schema)]


@pytest.fixture
def _fresh_router_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """A logger ``capture_logs`` can hear.

    The router's module logger, once bound by another test
    (``cache_logger_on_first_use``), keeps the processors of its day and slips
    past ``capture_logs`` -- the precedent of ``test_one_startup.py``. Which
    events the router writes does not change, only who hears them.
    """
    monkeypatch.setattr(
        "course_supporter.llm.stage_router.logger", structlog.get_logger()
    )


@pytest.mark.usefixtures("_fresh_router_logger")
class TestTheTrace:
    """(d) The register has no field for it: a log line, joined by input_hash."""

    async def test_a_seed_sent_is_logged_with_the_rows_input_hash(
        self, register: list[dict[str, Any]]
    ) -> None:
        with capture_logs() as logs:
            await run_and_capture(
                "dashscope",
                stage("dashscope", deterministic=True),
                with_schema=True,
                with_register=True,
            )

        lines = [e for e in logs if e["event"] == "stage_router_reproducibility"]
        assert len(lines) == 1
        line = lines[0]
        assert line["seed_sent"] is True
        assert line["seed"] == REPRODUCIBILITY_SEED
        assert line["temperature"] == 0.0
        assert (line["stage"], line["provider"]) == (STAGE_NAME, "dashscope")
        assert [row["input_hash"] for row in register] == [line["input_hash"]]

    async def test_a_seed_not_taken_is_logged_as_not_sent(
        self, register: list[dict[str, Any]]
    ) -> None:
        with capture_logs() as logs:
            await run_and_capture(
                "deepseek",
                stage("deepseek", deterministic=True),
                with_schema=True,
                with_register=True,
            )

        lines = [e for e in logs if e["event"] == "stage_router_reproducibility"]
        assert [(line["seed_sent"], line["seed"]) for line in lines] == [(False, None)]
        assert [row["input_hash"] for row in register] == [lines[0]["input_hash"]]

    @pytest.mark.parametrize("provider", list(RUNGS))
    async def test_no_line_without_the_flag(
        self, provider: str, register: list[dict[str, Any]]
    ) -> None:
        with capture_logs() as logs:
            await run_and_capture(
                provider, stage(provider), with_schema=True, with_register=True
            )

        assert not [e for e in logs if e["event"] == "stage_router_reproducibility"]
        assert len(register) == 1

    async def test_the_rows_input_hash_counts_the_seed(
        self, register: list[dict[str, Any]]
    ) -> None:
        # Same input, same rung: only the seed tells the two rows apart.
        for flags in ({}, {"deterministic": True}):
            await run_and_capture(
                "mistral",
                stage("mistral", **flags),
                with_schema=False,
                with_register=True,
            )
        plain, seeded = (row["input_hash"] for row in register)
        assert plain != seeded

    async def test_no_seed_no_new_hash(self, register: list[dict[str, Any]]) -> None:
        # Temperature 0 was already the input; without a seed nothing changed.
        for flags in ({}, {"deterministic": True}):
            await run_and_capture(
                "deepseek",
                stage("deepseek", **flags),
                with_schema=False,
                with_register=True,
            )
        plain, flagged = (row["input_hash"] for row in register)
        assert plain == flagged


class TestWhoTakesASeed:
    def test_exactly_the_documented_connectors(self) -> None:
        # DashScope and Mistral document a seed (read 2026-11-13); DeepSeek
        # documents none; Gemini, OpenAI and Anthropic were not read for it,
        # and a vendor not read counts as one that takes none.
        request = LLMRequest(prompt="p", model="m")
        takes = {
            name
            for name in PROVIDER_REGISTRY
            if connector(name).seed_supported(request)
        }
        assert takes == {"dashscope", "mistral"}

    def test_dashscope_multimodal_takes_none(self) -> None:
        # The seed is documented on the text endpoint; images go elsewhere.
        request = LLMRequest(prompt="p", model="m", contents=[b"\x89PNG\r\n\x1a\n"])
        assert connector("dashscope").seed_supported(request) is False

    @pytest.mark.parametrize(
        "name", sorted(set(PROVIDER_REGISTRY) - {"dashscope", "mistral"})
    )
    def test_a_seed_it_cannot_send_is_refused_before_the_call(self, name: str) -> None:
        request = LLMRequest(prompt="Answer in JSON.", model="m", seed=1)
        with pytest.raises(RequestConfigError, match="seed"):
            connector(name).check_request(request)

    def test_dashscope_refuses_a_seed_on_images(self) -> None:
        request = LLMRequest(prompt="p", model="m", contents=[b"\x89PNG"], seed=1)
        with pytest.raises(RequestConfigError, match="seed"):
            connector("dashscope").check_request(request)

    @pytest.mark.parametrize("name", ["dashscope", "mistral"])
    def test_a_seed_it_can_send_passes(self, name: str) -> None:
        connector(name).check_request(
            LLMRequest(prompt="Answer in JSON.", model="m", seed=1)
        )


class TestPathStagesCarryTheFlag:
    """The flag was dropped on the way to the router; now it arrives."""

    def test_every_model_stage_keeps_its_flag(self) -> None:
        stages = model_stages(load_path_config(_SUBMISSION_PATHS))
        carried = {
            name: router_stage_config(model_stage).deterministic
            for name, model_stage in stages.items()
        }
        assert carried == {name: s.deterministic for name, s in stages.items()}

    def test_the_evaluation_stages_are_deterministic(self) -> None:
        stages = model_stages(load_path_config(_SUBMISSION_PATHS))
        for name in ("criteria_evaluation", "project_criteria_evaluation"):
            assert router_stage_config(stages[name]).deterministic is True
        for name in ("review_explanation", "project_review_explanation"):
            assert router_stage_config(stages[name]).deterministic is False
