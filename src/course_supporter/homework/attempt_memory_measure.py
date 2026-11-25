"""The evaluation of a repeated attempt, with the earlier one's memory and without.

Purpose:
    Before the memory of task 13, K4 is calibrated live, the evaluation stage
    is asked about pairs of works — a student's first attempt and their second
    at the same task — and what comes back is written for the operator
    (``scripts/measure_attempt_memory.py``): the second attempt's verdicts
    with the memory (prompt v2) and without it (prompt v1), item by item
    against the first attempt's, apart for the items the change of the work
    touched and the items it did not; the stale quotes; the tokens and the
    price of every call. Nothing is written to a database and no register row
    is made: each call is one request to the stage's first rung, as
    :mod:`course_supporter.homework.explanation_measure` sends it.

Inputs:
    A pair is two recorded inputs of the evaluation's first request
    (``scripts/record_evaluation_input.py``: the prompt's variables) — of the
    first attempt and of the second. The task's texts of the two tell whether
    the task changed; their criteria whether the list did.

The calls of a pair — the same rung, the same seed
(:data:`~course_supporter.llm.reproducibility.REPRODUCIBILITY_SEED`: the
stage is deterministic, and ``explanation_measure.first_rung`` sends it as
the path does, the real connector willing):

    * ``first.v1`` — the first attempt with prompt v1, as production sent it;
      its verdicts settled by the quote check against its own work — the
      stored rows' approximation: no second request, a "met" whose quote is
      not in the work is unconfirmed;
    * ``second.v1`` — the second attempt with prompt v1, no memory;
    * ``second.v2`` — the second attempt with prompt v2 and the memory, built
      by production's own function
      (:func:`~course_supporter.homework.earlier_review.attempt_memory`) from
      the first's settled verdicts and the two works, with the path file's
      ``attempt_memory`` (the share, the lines of context), put under the
      ceiling of the request's growth by production's own
      (:func:`~course_supporter.homework.earlier_review.fit_memory`, the
      request rendered with the memory and without it), and rendered by the
      agent's own (``criteria_evaluator.memory_for_prompt``).
      A memory the ceiling drops leaves this request v2's without it.

The comparison, item by item of the second attempt (:func:`compare`): the
first's verdict, the verdict without the memory and with it — each settled
by the quote check against its work (``met``, ``unconfirmed``, ``not_met``) —
and where the item stands against the change of the work:

    * ``untouched`` — the item has a quote (in any of the three answers) and
      none of them is on a changed line; every item, when the work did not
      change;
    * ``changed`` — a quote is on a line the difference added, or the first
      attempt's quote is on a removed line and no longer in the work;
    * ``unlocated`` — no quote anywhere: the difference cannot place it;
    * ``no_difference`` — the memory shows no difference (rewritten, or none).

A stale quote is a ``met`` with the memory whose quote is the first work's
and not the second's — the risk the prompt's rules guard against.

A false history (``flip``; the operator's threshold "robust to a false
history", 2026-11-24): an item named to flip whose first verdict is ``met``
is shown in the memory as ``not_met`` — no quote, the sentence
:data:`FLIPPED_SENTENCE` — and nothing else changes: the first call and the
second without the memory are what they were, and the comparison keeps the
first's true word. Only the third call sees the lie. An item first ``not_met``
or ``unconfirmed`` is not flipped — it cannot be made ``met`` without a real
quote — and an item the pair has not is skipped; both are noted
(``flip_skipped``). Each flipped item is then judged (:func:`judged_by`):
by the work — with the memory as without it — or by the history it was
shown, and the summary counts them.

Dry run:
    No provider: every request is captured, nothing is sent. The first's
    verdicts are then placeholders (``not_met``, "dry run") — the second's
    request with the memory has its real size, not its real verdicts.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from course_supporter.agents.criteria_evaluator import (
    RESPONSE_SCHEMA,
    memory_for_prompt,
)
from course_supporter.criteria_kinds import CriteriaLayer, VerdictValue
from course_supporter.homework.criteria_verdicts import (
    EvaluationAnswer,
    SubmittedWork,
    read_answer,
)
from course_supporter.homework.doors import DoorReading
from course_supporter.homework.earlier_review import (
    AttemptMemory,
    FittedMemory,
    attempt_memory,
    fit_memory,
)
from course_supporter.homework.explanation_measure import (
    Sent,
    price_of,
    send_rendered_once,
)
from course_supporter.homework.path_stages import CRITERIA_EVALUATION
from course_supporter.homework.replies_measure import work_files
from course_supporter.llm.error_categories import StructuralRetryError
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.llm.reproducibility import REPRODUCIBILITY_SEED

if TYPE_CHECKING:
    import uuid

    from course_supporter.homework.path_config import ModelStage
    from course_supporter.llm.providers.base import LLMProvider
    from course_supporter.llm.registry import ModelRegistryConfig

SUMMARY_FORMAT: Final = "attempt_memory_measurement/1"
"""The ``format`` of the summary a measurement writes."""

STAGE_NAME: Final = CRITERIA_EVALUATION
"""The stage whose two prompts are measured."""

WITHOUT: Final = "prompts/criteria_evaluation/v1.md"
"""The calibrated prompt: no memory."""

WITH: Final = "prompts/criteria_evaluation/v2.md"
"""The prompt with the memory (task 13, K4)."""

DRY_RUN_SENTENCE: Final = "dry run"

FLIPPED_SENTENCE: Final = "(flipped for the measurement)"
"""The ``missing`` of a ``met`` shown as ``not_met`` by ``--flip``."""

_VERDICTS_SLOT: Final = re.compile(
    r"^<previous_attempt_verdicts>\n(.*?)\n</previous_attempt_verdicts>$",
    re.S | re.M,
)

_NOT_OPENED: Final = "\n\n=== NOT OPENED ("
_LAYER: Final = CriteriaLayer.MODEL


@dataclass(frozen=True, slots=True)
class Pair:
    """Two recorded inputs: the first attempt's and the second's."""

    label: str
    first: Mapping[str, Any]
    second: Mapping[str, Any]

    @property
    def task_changed(self) -> bool:
        """The task's texts differ between the two records."""
        keys = ("task_title", "task_description", "task_text")
        return any(self.first.get(k) != self.second.get(k) for k in keys)

    @property
    def criteria_changed(self) -> bool:
        """The criteria differ — another list, as the stage would see it."""
        return self.first.get("criteria") != self.second.get("criteria")


def load_pair(first: Path, second: Path, *, label: str) -> Pair:
    """A pair from two recorded inputs.

    Raises:
        ValueError: A record lacks a variable the request is rendered from.
    """
    records = []
    for path in (first, second):
        variables = json.loads(path.read_text(encoding="utf-8"))
        missing = {"items", "criteria", "submission_text"} - set(variables)
        if missing:
            msg = f"{path}: not a recorded evaluation input (no {sorted(missing)})"
            raise ValueError(msg)
        records.append(variables)
    return Pair(label=label, first=records[0], second=records[1])


def reading_of(variables: Mapping[str, Any]) -> DoorReading:
    """The work of a record as the doors read it, without the not-opened block.

    The snapshot is taken of the safety text; a record keeps the text the
    model read, so the block the doors append is taken off here.
    """
    text = str(variables["submission_text"])
    return DoorReading(text=text.split(_NOT_OPENED, 1)[0])


def submitted_work(variables: Mapping[str, Any]) -> SubmittedWork:
    """The record's work, file by file, for the quote check."""
    return SubmittedWork(work_files(str(variables["submission_text"])))


# ── The first attempt's verdicts, as the memory reads them ──


@dataclass(frozen=True, slots=True)
class SettledRow:
    """A first-attempt verdict as a stored row would carry it."""

    item_id: str
    verdict: str
    quote: str | None
    missing: str | None
    quote_not_found: bool
    criteria_layer: str
    criteria_source_id: uuid.UUID


def settled_rows(
    answer: EvaluationAnswer | None,
    items: Sequence[str],
    work: SubmittedWork,
    *,
    source_id: uuid.UUID,
) -> tuple[SettledRow, ...]:
    """The first answer settled by the quote check; placeholders without one."""
    said = {verdict.id: verdict for verdict in answer.verdicts} if answer else {}
    rows = []
    for item in items:
        verdict = said.get(item)
        if verdict is None:
            rows.append(
                SettledRow(
                    item, "not_met", None, DRY_RUN_SENTENCE, False, _LAYER, source_id
                )
            )
            continue
        met = verdict.verdict is VerdictValue.MET
        found = met and verdict.quote is not None and work.find(verdict.quote)
        rows.append(
            SettledRow(
                item_id=item,
                verdict="met" if found else "not_met",
                quote=verdict.quote if met else None,
                missing=None if met else verdict.missing,
                quote_not_found=met and not found,
                criteria_layer=_LAYER.value,
                criteria_source_id=source_id,
            )
        )
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class Flip:
    """What ``--flip`` did to one item of one pair.

    Attributes:
        item: The item named.
        first: The first attempt's true word (``met``, ``not_met``,
            ``unconfirmed``); ``None`` when the pair has no such item.
        skipped: ``None`` — flipped; ``"not_met"`` — first not met or
            unconfirmed, left as it was; ``"absent"`` — not in the pair.
        dry_run: The first verdict was a dry run's placeholder, flipped all
            the same so the request shows the flipped item.
    """

    item: str
    first: str | None
    skipped: str | None
    dry_run: bool = False


def flip_rows(
    rows: Sequence[SettledRow], items: Sequence[str]
) -> tuple[tuple[SettledRow, ...], list[Flip]]:
    """The rows the memory is built from, the named ``met`` items flipped.

    A ``met`` becomes ``not_met`` without its quote, with
    :data:`FLIPPED_SENTENCE`; a ``not_met`` or ``unconfirmed`` stays; an item
    the rows have not is skipped. A dry run's placeholder (no answer) is
    flipped too, so the captured request shows it.
    """
    by_id = {row.item_id: row for row in rows}
    flips: list[Flip] = []
    flipped: dict[str, SettledRow] = {}
    for item in dict.fromkeys(items):
        row = by_id.get(item)
        if row is None:
            flips.append(Flip(item, None, "absent"))
            continue
        placeholder = row.missing == DRY_RUN_SENTENCE and row.verdict == "not_met"
        if row.verdict != "met" and not placeholder:
            flips.append(Flip(item, _first_word(row), "not_met"))
            continue
        flipped[item] = replace(
            row,
            verdict="not_met",
            quote=None,
            missing=FLIPPED_SENTENCE,
            quote_not_found=False,
        )
        flips.append(Flip(item, _first_word(row), None, dry_run=placeholder))
    return tuple(flipped.get(row.item_id, row) for row in rows), flips


def memory_of(
    pair: Pair,
    rows: Sequence[SettledRow],
    *,
    rewritten_share: float,
    context_lines: int,
    list_now: uuid.UUID,
    list_then: uuid.UUID,
) -> AttemptMemory | None:
    """The memory the stage would read for the second attempt, before the ceiling.

    ``list_then`` is the first's list; the stand gives it another address
    than ``list_now`` exactly when the records' criteria differ.
    """
    return attempt_memory(
        list_then,
        rows,
        criteria_layer=_LAYER,
        criteria_source_id=list_now,
        judged=list(pair.second["items"]),
        task_changed=pair.task_changed,
        earlier_work=reading_of(pair.first),
        current_work=reading_of(pair.second),
        rewritten_share=rewritten_share,
        context_lines=context_lines,
    )


def rendered_chars(prompt_ref: str, variables: Mapping[str, Any]) -> int:
    """Characters of a request rendered from recorded variables: system + user.

    The measure of :func:`~course_supporter.agents.criteria_evaluator.request_chars`
    on a record: the recorded variables are what ``render_context`` gave.
    """
    rendered = load_prompt(prompt_ref).render(**variables)
    return len(rendered.system or "") + len(rendered.user or "")


def fitted_memory(
    pair: Pair, memory: AttemptMemory | None, *, max_growth: float
) -> FittedMemory | None:
    """The memory under the ceiling, the second request rendered by v2."""
    if memory is None:
        return None
    return fit_memory(
        memory,
        request_chars=lambda shown: rendered_chars(
            WITH, with_memory(pair.second, shown)
        ),
        max_growth=max_growth,
    )


def with_memory(
    variables: Mapping[str, Any], memory: AttemptMemory | None
) -> dict[str, Any]:
    """The second request's variables with the memory, as the stage adds it."""
    shown = dict(variables)
    if memory is not None:
        shown["previous_attempt"] = memory_for_prompt(memory)
    return shown


# ── The comparison ──


@dataclass(frozen=True, slots=True)
class ItemComparison:
    """One item of the second attempt, three ways."""

    item: str
    place: str
    first: str
    without: str | None
    with_memory: str | None
    stale_quote: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "item": self.item,
            "place": self.place,
            "first": self.first,
            "without": self.without,
            "with_memory": self.with_memory,
            "stale_quote": self.stale_quote,
        }


def settled_word(
    answer: EvaluationAnswer | None, item: str, work: SubmittedWork
) -> tuple[str | None, str | None]:
    """The item's settled word in ``answer`` and its quote; ``None`` without one."""
    if answer is None:
        return None, None
    verdict = next((v for v in answer.verdicts if v.id == item), None)
    if verdict is None:
        return None, None
    if verdict.verdict is not VerdictValue.MET:
        return "not_met", None
    quote = verdict.quote
    found = quote is not None and work.find(quote) is not None
    return ("met" if found else "unconfirmed"), quote


def _first_word(row: SettledRow) -> str:
    if row.verdict == "met":
        return "met"
    return "unconfirmed" if row.quote_not_found else "not_met"


def _changed_lines(diff: str) -> tuple[list[str], list[str]]:
    added, removed = [], []
    for line in diff.splitlines():
        if line.startswith(("+++ ", "--- previous")):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


def _place(
    memory: AttemptMemory | None, quotes: Sequence[str], second: SubmittedWork
) -> str:
    if memory is None or memory.diff is None:
        return "no_difference"
    if memory.diff == "":
        return "untouched"
    if not quotes:
        return "unlocated"
    added, removed = _changed_lines(memory.diff)
    for quote in quotes:
        text = quote.strip()
        if any(text in line for line in added):
            return "changed"
        if any(text in line for line in removed) and second.find(quote) is None:
            return "changed"
    return "untouched"


def compare(
    pair: Pair,
    rows: Sequence[SettledRow],
    memory: AttemptMemory | None,
    without: EvaluationAnswer | None,
    with_: EvaluationAnswer | None,
) -> list[ItemComparison]:
    """Every item of the second attempt: the first's word, without, with."""
    first_work = submitted_work(pair.first)
    second_work = submitted_work(pair.second)
    by_id = {row.item_id: row for row in rows}
    compared = []
    for item in pair.second["items"]:
        row = by_id.get(item)
        if row is None:
            continue
        word_without, quote_without = settled_word(without, item, second_work)
        word_with, quote_with = settled_word(with_, item, second_work)
        quotes = [q for q in (row.quote, quote_without, quote_with) if q]
        stale = (
            quote_with is not None
            and first_work.find(quote_with) is not None
            and second_work.find(quote_with) is None
        )
        compared.append(
            ItemComparison(
                item=item,
                place=_place(memory, quotes, second_work),
                first=_first_word(row),
                without=word_without,
                with_memory=word_with,
                stale_quote=stale,
            )
        )
    return compared


def tally(compared: Sequence[ItemComparison]) -> dict[str, dict[str, int]]:
    """By place: items, and how often each answer kept the first's word."""
    out: dict[str, Counter[str]] = {}
    for item in compared:
        counts = out.setdefault(item.place, Counter())
        counts["items"] += 1
        counts["kept_with_memory"] += item.with_memory == item.first
        counts["kept_without"] += item.without == item.first
        counts["with_equals_without"] += item.with_memory == item.without
        counts["stale_quotes"] += item.stale_quote
    return {place: dict(counts) for place, counts in sorted(out.items())}


# ── The calls ──


@dataclass(frozen=True, slots=True)
class Call:
    """One request of the stand, priced."""

    label: str
    prompt_ref: str
    sent: Sent
    content: str | None
    cost_usd: Decimal | None
    cost_kind: str | None

    def as_json(self) -> dict[str, Any]:
        request, response = self.sent.request, self.sent.response
        return {
            "label": self.label,
            "prompt_ref": self.prompt_ref,
            "provider": self.sent.provider,
            "model": request.model if request is not None else None,
            "seed": REPRODUCIBILITY_SEED,
            "request_hash": self.sent.request_hash,
            "request_chars": (
                None
                if request is None
                else len(request.system_prompt or "") + len(request.prompt)
            ),
            "failure": self.sent.failure
            or (None if self.sent.raised is None else str(self.sent.raised)),
            "tokens_in": response.tokens_in if response is not None else None,
            "tokens_out": response.tokens_out if response is not None else None,
            "tokens_reasoning": (
                response.tokens_reasoning if response is not None else None
            ),
            "cost_usd": None if self.cost_usd is None else str(self.cost_usd),
            "cost_kind": self.cost_kind,
        }


async def _send(
    label: str,
    variables: Mapping[str, Any],
    prompt_ref: str,
    *,
    stage: ModelStage,
    registry: ModelRegistryConfig,
    provider: LLMProvider | None,
) -> Call:
    sent = await send_rendered_once(
        variables,
        prompt_ref,
        stage=stage,
        stage_name=STAGE_NAME,
        response_schema=RESPONSE_SCHEMA,
        registry=registry,
        provider=provider,
    )
    cost_usd, cost_kind, _ = price_of(sent, registry=registry)
    content = sent.response.content if sent.response is not None else None
    return Call(label, prompt_ref, sent, content, cost_usd, cost_kind)


def answer_of(call: Call, items: Sequence[str]) -> EvaluationAnswer | None:
    """The call's answer read as the stage reads it; ``None`` when it cannot be."""
    if not call.content or call.sent.response is None:
        return None
    try:
        return read_answer(call.content, items)
    except StructuralRetryError:
        return None


@dataclass
class PairResult:
    """Everything measured on one pair."""

    pair: Pair
    calls: list[Call] = field(default_factory=list)
    memory: AttemptMemory | None = None
    fitted: FittedMemory | None = None
    compared: list[ItemComparison] = field(default_factory=list)
    flips: list[Flip] = field(default_factory=list)

    @property
    def answered(self) -> bool:
        return all(call.content and call.sent.response for call in self.calls)

    @property
    def cost_usd(self) -> Decimal:
        return sum(
            (call.cost_usd for call in self.calls if call.cost_usd is not None),
            Decimal(0),
        )


async def measure_pair(
    pair: Pair,
    *,
    stage: ModelStage,
    registry: ModelRegistryConfig,
    provider: LLMProvider | None,
    rewritten_share: float,
    context_lines: int,
    max_growth: float,
    list_now: uuid.UUID,
    list_then: uuid.UUID,
    flip: Sequence[str] = (),
) -> PairResult:
    """The three calls of a pair, and the comparison.

    ``flip`` — the items whose ``met`` the memory shows as ``not_met``
    (:func:`flip_rows`); the first two calls and the comparison keep the
    first's true verdicts.
    """
    result = PairResult(pair)
    first_items = list(pair.first["items"])
    second_items = list(pair.second["items"])
    first = await _send(
        "first.v1",
        pair.first,
        WITHOUT,
        stage=stage,
        registry=registry,
        provider=provider,
    )
    result.calls.append(first)
    rows = settled_rows(
        answer_of(first, first_items),
        first_items,
        submitted_work(pair.first),
        source_id=list_then if pair.criteria_changed else list_now,
    )
    shown_rows, result.flips = flip_rows(rows, flip)
    result.fitted = fitted_memory(
        pair,
        memory_of(
            pair,
            shown_rows,
            rewritten_share=rewritten_share,
            context_lines=context_lines,
            list_now=list_now,
            list_then=list_then,
        ),
        max_growth=max_growth,
    )
    result.memory = result.fitted.memory if result.fitted is not None else None
    without = await _send(
        "second.v1",
        pair.second,
        WITHOUT,
        stage=stage,
        registry=registry,
        provider=provider,
    )
    with_ = await _send(
        "second.v2",
        with_memory(pair.second, result.memory),
        WITH,
        stage=stage,
        registry=registry,
        provider=provider,
    )
    result.calls.extend([without, with_])
    result.compared = compare(
        pair,
        rows,
        result.memory,
        answer_of(without, second_items),
        answer_of(with_, second_items),
    )
    return result


# ── What the measurement keeps ──


def summary_of(results: Sequence[PairResult], *, dry_run: bool) -> dict[str, Any]:
    """The summary: per pair, and in all."""
    every = [item for result in results for item in result.compared]
    return {
        "format": SUMMARY_FORMAT,
        "dry_run": dry_run,
        "seed": REPRODUCIBILITY_SEED,
        "prompts": {"without": WITHOUT, "with": WITH},
        "pairs": [_pair_json(result) for result in results],
        "tally": tally(every),
        "flips": flip_tally(
            [entry for result in results for entry in flip_entries(result)]
        ),
        "cost_usd_total": str(sum((r.cost_usd for r in results), Decimal(0))),
    }


def _pair_json(result: PairResult) -> dict[str, Any]:
    memory, fitted = result.memory, result.fitted
    calls = {call.label: call for call in result.calls}
    without, with_ = calls.get("second.v1"), calls.get("second.v2")
    return {
        "label": result.pair.label,
        "task_changed": result.pair.task_changed,
        "criteria_changed": result.pair.criteria_changed,
        "ceiling": (
            None
            if fitted is None
            else {
                "reason": fitted.reason,
                "base_chars": fitted.base_chars,
                "growth": round(fitted.growth, 4),
                "sent_growth": round(fitted.sent_growth, 4),
            }
        ),
        "memory": (
            None
            if memory is None
            else {
                "verdicts": None if memory.verdicts is None else len(memory.verdicts),
                "diff_chars": None if memory.diff is None else len(memory.diff),
                "changed_share": memory.changed_share,
                "rewritten": memory.rewritten,
                "earlier_work_unavailable": memory.earlier_work_unavailable,
            }
        ),
        "input_growth": _growth(without, with_),
        "calls": [call.as_json() for call in result.calls],
        "items": [item.as_json() for item in result.compared],
        "tally": tally(result.compared),
        "flips": flip_entries(result),
    }


def shown_in_request(call: Call | None, item: str) -> dict[str, Any] | None:
    """The item's entry in the ``<previous_attempt_verdicts>`` of the request
    the call sent (or captured); ``None`` when the request shows none."""
    request = call.sent.request if call is not None else None
    if request is None:
        return None
    found = _VERDICTS_SLOT.search(request.prompt)
    if found is None:
        return None
    entries = json.loads(found.group(1))
    return next((entry for entry in entries if entry.get("id") == item), None)


def judged_by(*, shown: str | None, without: str | None, with_: str | None) -> str:
    """How the answer with the memory treated a flipped item.

    ``work`` — as without the memory, against the history shown;
    ``history`` — as the history shown, against the answer without it;
    ``indistinct`` — all three agree, so the flip proved nothing;
    ``not_shown`` — the request did not show the item; ``no_answer`` — a
    call gave none; ``other`` — anything else.
    """
    if shown is None:
        return "not_shown"
    if without is None or with_ is None:
        return "no_answer"
    if with_ == without:
        return "indistinct" if without == shown else "work"
    return "history" if with_ == shown else "other"


def flip_entries(result: PairResult) -> list[dict[str, Any]]:
    """Every ``--flip`` of the pair: what was true, shown, and answered."""
    calls = {call.label: call for call in result.calls}
    compared = {item.item: item for item in result.compared}
    entries: list[dict[str, Any]] = []
    for flip in result.flips:
        if flip.skipped is not None:
            entries.append(
                {"item": flip.item, "first": flip.first, "flip_skipped": flip.skipped}
            )
            continue
        entry = shown_in_request(calls.get("second.v2"), flip.item)
        shown = None if entry is None else str(entry.get("verdict"))
        item = compared.get(flip.item)
        without = item.without if item is not None else None
        with_ = item.with_memory if item is not None else None
        entries.append(
            {
                "item": flip.item,
                "first": flip.first,
                "shown_in_memory": shown,
                "shown_missing": None if entry is None else entry.get("missing"),
                "second_without_memory": without,
                "second_with_memory": with_,
                "judged_by": judged_by(shown=shown, without=without, with_=with_),
                "dry_run": flip.dry_run,
            }
        )
    return entries


def flip_tally(entries: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """How many flipped items were judged by the work, by the history, …"""
    counts: Counter[str] = Counter()
    for entry in entries:
        if "flip_skipped" in entry:
            counts[f"skipped_{entry['flip_skipped']}"] += 1
        else:
            counts["flipped"] += 1
            counts[f"judged_by_{entry['judged_by']}"] += 1
    return dict(sorted(counts.items()))


def _growth(without: Call | None, with_: Call | None) -> dict[str, Any] | None:
    """What the memory added to the second request: characters, and tokens."""
    if without is None or with_ is None:
        return None
    a, b = without.as_json(), with_.as_json()
    tokens = (
        None
        if a["tokens_in"] is None or b["tokens_in"] is None
        else b["tokens_in"] - a["tokens_in"]
    )
    chars = (
        None
        if a["request_chars"] is None or b["request_chars"] is None
        else b["request_chars"] - a["request_chars"]
    )
    return {
        "chars": chars,
        "tokens_estimated": None if chars is None else round(chars / 3.5),
        "tokens_in": tokens,
        "share_of_input": (
            None
            if chars is None or not a["request_chars"]
            else round(chars / a["request_chars"], 4)
        ),
    }


def write_measurement(
    out_dir: Path, results: Sequence[PairResult], *, dry_run: bool
) -> Path:
    """Each answer, and ``summary.json``; returns the summary's path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for result in results:
        for call in result.calls:
            if call.content is None or call.sent.response is None:
                continue
            target = out_dir / f"{result.pair.label}.{call.label}.answer.json"
            try:
                parsed = json.loads(call.content)
            except json.JSONDecodeError:
                target.with_suffix(".txt").write_text(call.content, encoding="utf-8")
            else:
                target.write_text(
                    json.dumps(parsed, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
    path = out_dir / "summary.json"
    path.write_text(
        json.dumps(summary_of(results, dry_run=dry_run), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return path
