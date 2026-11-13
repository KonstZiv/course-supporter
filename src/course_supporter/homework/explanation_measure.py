"""Two prompts of the explanation stage, measured on one recorded input.

Purpose:
    Before a new prompt of ``review_explanation`` replaces the calibrated one,
    both are asked the same question: a recorded input of a real submission,
    rendered with each prompt and sent once to the stage's first rung. What
    comes back — the answer, the tokens, the price from the registry, and
    whether the answer agrees with the facts — is written side by side for
    the operator to read (``scripts/measure_explanation_prompt.py``).

The recorded input:
    A JSON file of the input the stage hands
    :class:`~course_supporter.agents.review_explainer.ReviewExplainerAgent`
    (:class:`~course_supporter.agents.review_explainer.ExplanationInput`):
    the task's texts, the facts criterion by criterion, the work as the doors
    read it, the language and the course links shown. The prompt's variables
    are derived from it by the agent's own
    :func:`~course_supporter.agents.review_explainer.render_context`, so every
    prompt is rendered from exactly what the stage would render it from; and
    the facts let each answer be checked as the stage checks it.
    :func:`record_of` writes one, :func:`input_of` reads one back;
    ``homework/explanation_input_rebuild.py`` rebuilds one from a
    submission's rows.

The request:
    Built by the real router (:class:`~course_supporter.llm.stage_router.StageRouter`)
    from the stage's own description in ``config/submission_paths.yaml`` —
    its first rung alone, pinned to the stage's output ceiling as the path
    pins it (``homework/path_stages.py::_execution``), under its money
    ceiling — with the agent's schema and JSON flag. No validator and no
    retry: one call per prompt. :func:`send_once` with no provider sends
    nothing and only captures the request (a test, a dry run, the hash
    against the call register).

Price:
    The provider's token counts priced by the registry
    (:func:`~course_supporter.llm.pricing.price_token_call`), as the router
    prices an attempt for its register row. No register row is written.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from course_supporter.agents.review_explainer import (
    RESPONSE_SCHEMA,
    ExplanationInput,
    ShownLink,
    render_context,
)
from course_supporter.criteria_kinds import VerdictValue
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.criteria_verdicts import QuotePlace
from course_supporter.homework.verdict_explanation import (
    CriterionFacts,
    ExplanationFacts,
    JudgedItem,
    read_explanation,
)
from course_supporter.llm.error_categories import (
    ErrorCategory,
    LadderExhaustedError,
    StructuralRetryError,
)
from course_supporter.llm.input_hash import hash_attempt_input
from course_supporter.llm.ladder_config import LadderConfig, LadderEntry, StageConfig
from course_supporter.llm.pricing import TokenUsage, price_token_call
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.schemas import LLMRequest, LLMResponse
from course_supporter.llm.stage_router import StageExecution, StageRouter
from course_supporter.llm.token_budget import (
    estimate_request_input_tokens,
    estimate_tokens,
)
from course_supporter.models.review_structure import Position

if TYPE_CHECKING:
    from course_supporter.homework.path_config import ModelStage
    from course_supporter.llm.registry import ModelRegistryConfig

RECORD_FORMAT: Final = "review_explanation_input/1"
"""The ``format`` of a recorded input; another shape takes another value."""

SUMMARY_FORMAT: Final = "review_explanation_measurement/1"
"""The ``format`` of the summary a measurement writes."""

STAGE_NAME: Final = "review_explanation"
"""The stage whose prompts are measured."""

PROMPT_REFS: Final = (
    "prompts/review_explanation/v2.md",
    "prompts/review_explanation/v3.md",
)
"""The calibrated prompt and its successor, in that order."""


# ── The recorded input ──


def record_of(
    shown: ExplanationInput,
    *,
    submission_id: str | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The recorded input as a JSON-ready mapping.

    ``provenance`` is what the recorder knew about where the input came from
    — free-form, carried to the measurement's summary unread.
    """
    return {
        "format": RECORD_FORMAT,
        "submission_id": submission_id,
        "provenance": dict(provenance or {}),
        "input": {
            "task_title": shown.task_title,
            "task_description": shown.task_description,
            "task_text": shown.task_text,
            "facts": {
                "passed": shown.facts.passed,
                "score": shown.facts.score,
                "criteria": [_criterion_json(c) for c in shown.facts.criteria],
            },
            "submission_text": shown.submission_text,
            "language": shown.language,
            "course_links": {
                criterion_id: [_link_json(link) for link in links]
                for criterion_id, links in shown.course_links.items()
            },
        },
    }


def input_of(record: Mapping[str, Any]) -> ExplanationInput:
    """The agent's input from a recorded one.

    Raises:
        ValueError: The record is not of :data:`RECORD_FORMAT`, or a value in
            it does not fit its field.
    """
    if record.get("format") != RECORD_FORMAT:
        msg = f"not a recorded explanation input: format {record.get('format')!r}"
        raise ValueError(msg)
    data = record["input"]
    facts = data["facts"]
    return ExplanationInput(
        task_title=data["task_title"],
        task_description=data["task_description"],
        task_text=data["task_text"],
        facts=ExplanationFacts(
            passed=bool(facts["passed"]),
            score=int(facts["score"]),
            criteria=tuple(_criterion_of(c) for c in facts["criteria"]),
        ),
        submission_text=data["submission_text"],
        language=data["language"],
        course_links={
            criterion_id: tuple(_link_of(link) for link in links)
            for criterion_id, links in data["course_links"].items()
        },
    )


def _criterion_json(facts: CriterionFacts) -> dict[str, Any]:
    return {
        "criterion": facts.criterion.model_dump(mode="json"),
        "verdict": facts.verdict.value,
        "judged": None if facts.judged is None else _item_json(facts.judged),
        "points": [_item_json(point) for point in facts.points],
    }


def _criterion_of(data: Mapping[str, Any]) -> CriterionFacts:
    judged = data["judged"]
    return CriterionFacts(
        criterion=Criterion.model_validate(data["criterion"]),
        verdict=VerdictValue(data["verdict"]),
        judged=None if judged is None else _item_of(judged),
        points=tuple(_item_of(point) for point in data["points"]),
    )


def _item_json(item: JudgedItem) -> dict[str, Any]:
    place = item.place
    return {
        "id": item.id,
        "verdict": item.verdict.value,
        "quote": item.quote,
        "place": (
            None
            if place is None
            else {
                "file": place.file,
                "lines": None if place.lines is None else list(place.lines),
            }
        ),
        "missing": item.missing,
        "quote_not_found": item.quote_not_found,
        "kept_from_earlier": item.kept_from_earlier,
        "quote_from_base": item.quote_from_base,
    }


def _item_of(data: Mapping[str, Any]) -> JudgedItem:
    place = data["place"]
    lines = None if place is None else place["lines"]
    return JudgedItem(
        id=data["id"],
        verdict=VerdictValue(data["verdict"]),
        quote=data["quote"],
        place=(
            None
            if place is None
            else QuotePlace(
                place["file"], None if lines is None else (lines[0], lines[1])
            )
        ),
        missing=data["missing"],
        quote_not_found=bool(data["quote_not_found"]),
        kept_from_earlier=bool(data["kept_from_earlier"]),
        quote_from_base=bool(data["quote_from_base"]),
    )


def _link_json(link: ShownLink) -> dict[str, Any]:
    return {
        "id": link.id,
        "material": link.material,
        "position": (
            None if link.position is None else link.position.model_dump(mode="json")
        ),
        "excerpt": link.excerpt,
    }


def _link_of(data: Mapping[str, Any]) -> ShownLink:
    position = data["position"]
    return ShownLink(
        id=data["id"],
        material=data["material"],
        position=None if position is None else Position.model_validate(position),
        excerpt=data["excerpt"],
    )


# ── One request, as the stage's first rung is sent it ──


def first_rung(stage: ModelStage, prompt_ref: str) -> StageConfig:
    """The stage as the path runs it, cut to its first rung, with ``prompt_ref``.

    The translation of ``homework/path_stages.py::_execution``: an unpinned
    rung takes the stage's output ceiling.
    """
    rung = stage.ladder[0]
    return StageConfig(
        prompt_ref=prompt_ref,
        requires=list(stage.requires),
        ladder=[
            LadderEntry(
                provider=rung.provider,
                model=rung.model,
                reasoning=rung.reasoning,
                max_output_tokens=rung.max_output_tokens
                or stage.ceilings.output_tokens,
            )
        ],
        input_budget_ratio=stage.input_budget_ratio,
        record_output=stage.record_output,
    )


@dataclass(frozen=True, slots=True)
class Sent:
    """One prompt, sent once (or only captured).

    Attributes:
        prompt_ref: The prompt rendered.
        provider: The rung's provider, as the ladder names it.
        request: What went on the wire; ``None`` when the router refused to
            send it (the input budget, the money ceiling).
        response: What came back; ``None`` without a call or on an error.
        started_at: When the request was sent.
        raised: What the provider raised, if it raised.
        failure: Why the router gave up on the answer, if it did — an empty
            or cut-off answer is still a ``response`` with its tokens.
    """

    prompt_ref: str
    provider: str
    request: LLMRequest | None
    response: LLMResponse | None
    started_at: datetime | None
    raised: BaseException | None
    failure: str | None

    @property
    def request_hash(self) -> str | None:
        """The request's hash, as the call register stores it."""
        if self.request is None:
            return None
        return hash_attempt_input(self.request, provider=self.provider)


class _Spy(LLMProvider):
    """A provider that keeps what it was sent and what it answered.

    Over a real provider it passes the call on; without one it answers
    ``{}`` and nothing leaves the process.
    """

    def __init__(self, inner: LLMProvider | None) -> None:
        super().__init__()
        self.inner = inner
        self.request: LLMRequest | None = None
        self.response: LLMResponse | None = None
        self.raised: BaseException | None = None
        self.started_at: datetime | None = None
        if inner is not None:
            self.provider_name = inner.provider_name
            self.default_max_output_tokens = inner.default_max_output_tokens

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.request = request
        self.started_at = datetime.now(UTC)
        if self.inner is None:
            return LLMResponse(content="{}", provider="capture", model_id=request.model)
        try:
            self.response = await self.inner.complete(request)
        except Exception as exc:
            self.raised = exc
            raise
        return self.response

    def check_request(self, request: LLMRequest) -> None:
        if self.inner is not None:
            self.inner.check_request(request)

    def classify_error(self, exc: Exception) -> ErrorCategory:
        if self.inner is None:
            return ErrorCategory.SEMANTIC
        return self.inner.classify_error(exc)


async def send_once(
    shown: ExplanationInput,
    prompt_ref: str,
    *,
    stage: ModelStage,
    registry: ModelRegistryConfig,
    provider: LLMProvider | None,
    prompt_base_path: Path | None = None,
) -> Sent:
    """Render ``prompt_ref`` from ``shown`` and send it once to the first rung.

    ``provider`` ``None`` sends nothing: the request is captured, answered
    with ``{}``, and returned — no network, no key.

    Raises:
        RuntimeError: Nothing was captured — the router refused to send the
            request (only raised when there is no provider to blame).
    """
    config = first_rung(stage, prompt_ref)
    entry = config.ladder[0]
    spy = _Spy(provider)
    router = StageRouter(
        LadderConfig(),
        {entry.provider: spy},
        registry=registry,
        prompt_base_path=prompt_base_path,
        max_retries_infrastructure=0,
    )
    execution = StageExecution(
        stage=config,
        stage_name=STAGE_NAME,
        stop_on_output_ceiling=True,
        money_ceiling_usd=stage.ceilings.money_usd,
    )
    failure: str | None = None
    try:
        await execution.run(
            router,
            expects_json=True,
            response_schema=RESPONSE_SCHEMA,
            **render_context(shown),
        )
    except LadderExhaustedError as exhausted:
        failure = str(exhausted)
    if provider is None and spy.request is None:
        msg = f"the router sent no request for {prompt_ref}: {failure}"
        raise RuntimeError(msg)
    return Sent(
        prompt_ref=prompt_ref,
        provider=entry.provider,
        request=spy.request,
        response=spy.response,
        started_at=spy.started_at,
        raised=spy.raised,
        failure=failure,
    )


# ── What the measurement keeps ──


@dataclass(frozen=True, slots=True)
class Measured:
    """One prompt's call, priced and checked.

    Attributes:
        label: The prompt's file stem (``v2``).
        check: ``"agrees"`` when the answer agrees with the facts as the stage
            checks it, ``"refused: <reason>"`` when the stage would retry it,
            ``"no answer"`` without one.
    """

    label: str
    prompt_ref: str
    provider: str
    model: str
    request_hash: str | None
    content: str | None
    failure: str | None
    tokens_in: int | None
    tokens_in_cached: int | None
    tokens_out: int | None
    tokens_reasoning: int | None
    finish_reason: str | None
    latency_ms: int | None
    cost_usd: Decimal | None
    cost_kind: str | None
    tariff_window: str | None
    check: str

    def as_json(self) -> dict[str, Any]:
        """The summary's entry: everything but the answer's text."""
        return {
            "label": self.label,
            "prompt_ref": self.prompt_ref,
            "provider": self.provider,
            "model": self.model,
            "request_hash": self.request_hash,
            "failure": self.failure,
            "tokens_in": self.tokens_in,
            "tokens_in_cached": self.tokens_in_cached,
            "tokens_out": self.tokens_out,
            "tokens_reasoning": self.tokens_reasoning,
            "finish_reason": self.finish_reason,
            "latency_ms": self.latency_ms,
            "cost_usd": None if self.cost_usd is None else str(self.cost_usd),
            "cost_kind": self.cost_kind,
            "tariff_window": self.tariff_window,
            "check": self.check,
        }


def measured(
    sent: Sent, facts: ExplanationFacts, *, registry: ModelRegistryConfig
) -> Measured:
    """Price ``sent`` by the registry and check its answer against ``facts``."""
    request, response = sent.request, sent.response
    model = request.model if request is not None else ""
    if request is None or sent.started_at is None:
        cost_usd: Decimal | None = None
        cost_kind: str | None = None
        window: str | None = None
    else:
        priced = price_token_call(
            registry.models.get(model),
            catalog_version=registry.catalog_version,
            started_at=sent.started_at,
            usage=(
                TokenUsage(
                    tokens_in=response.tokens_in,
                    tokens_in_cached=response.tokens_in_cached,
                    tokens_out=response.tokens_out,
                )
                if response is not None
                else None
            ),
            failure=sent.raised,
            input_estimate=estimate_request_input_tokens(request),
            output_estimate=(
                estimate_tokens(response.content) if response is not None else None
            ),
        )
        cost_usd = priced.cost_usd
        cost_kind = priced.cost_kind.value
        window = None if priced.tariff_window is None else priced.tariff_window.value
    content = response.content if response is not None else None
    return Measured(
        label=Path(sent.prompt_ref).stem,
        prompt_ref=sent.prompt_ref,
        provider=sent.provider,
        model=model,
        request_hash=sent.request_hash,
        content=content,
        failure=sent.failure or (None if sent.raised is None else str(sent.raised)),
        tokens_in=response.tokens_in if response is not None else None,
        tokens_in_cached=response.tokens_in_cached if response is not None else None,
        tokens_out=response.tokens_out if response is not None else None,
        tokens_reasoning=response.tokens_reasoning if response is not None else None,
        finish_reason=response.finish_reason.value if response is not None else None,
        latency_ms=response.latency_ms if response is not None else None,
        cost_usd=cost_usd,
        cost_kind=cost_kind,
        tariff_window=window,
        check=_check(content, facts),
    )


def _check(content: str | None, facts: ExplanationFacts) -> str:
    if not content:
        return "no answer"
    try:
        read_explanation(content, facts)
    except StructuralRetryError as refusal:
        return f"refused: {refusal.feedback}"
    return "agrees"


def same_user_message(sent: Sequence[Sent]) -> bool:
    """Whether every prompt sent the same user message, byte for byte."""
    users = {s.request.prompt for s in sent if s.request is not None}
    return len(users) == 1 and all(s.request is not None for s in sent)


def write_measurement(
    out_dir: Path,
    results: Sequence[Measured],
    *,
    record: Mapping[str, Any],
    same_user: bool,
) -> Path:
    """Write each answer and the summary into ``out_dir``; return the summary.

    ``<label>.answer.json`` is the answer re-indented when it is JSON,
    ``<label>.answer.txt`` the raw text when it is not; a call without an
    answer writes neither. ``summary.json`` holds the tokens, the price, the
    check and the recorder's provenance.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for result in results:
        if result.content is None:
            continue
        try:
            parsed = json.loads(result.content)
        except json.JSONDecodeError:
            (out_dir / f"{result.label}.answer.txt").write_text(
                result.content, encoding="utf-8"
            )
        else:
            (out_dir / f"{result.label}.answer.json").write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    total = [r.cost_usd for r in results if r.cost_usd is not None]
    summary = {
        "format": SUMMARY_FORMAT,
        "submission_id": record.get("submission_id"),
        "provenance": record.get("provenance", {}),
        "user_message_identical": same_user,
        "calls": [r.as_json() for r in results],
        "cost_usd_total": str(sum(total, Decimal(0))) if total else None,
    }
    path = out_dir / "summary.json"
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path
