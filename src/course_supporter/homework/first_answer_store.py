"""The evaluation stage's first answer, kept in Redis while the task is retried.

Purpose:
    The evaluation stage (:mod:`course_supporter.homework.criteria_evaluation`)
    pays for a first request about every item, and then — for the items whose
    verdict cannot stand — a second one to the rung that answered. When that
    second request gets no answer the body re-queues the whole task
    (``path_runner._stage_produced_nothing``), and its checkpoint says only
    "the stage is not done": the next pass pays for the first request again and
    the answer already paid for is thrown away. This module keeps that answer
    for the length of the retries, so the next pass begins at the second
    request.

Interface:
    :func:`first_answer_key` — the key of one stage of one submission over one
    input, and :func:`first_answer_ttl_s` — how long a value lives.

    :class:`FirstAnswerStore` — ``recall`` / ``keep`` / ``forget`` over one key,
    and ``discard`` for a value the stage refuses. None of them raises: the
    store is a help, never a condition (see "Failure").

The key:
    ``cs:first_answer:<submission_id>:<stage_name>:<digest>``. The digest is the
    SHA-256 of the canonical JSON of what the model is shown
    (:class:`~course_supporter.agents.criteria_evaluator.EvaluationInput`:
    the task, the criteria, the work, the replies, the earlier attempt's
    memory) followed by the stage's description
    (``StageConfig.model_dump_json()``: prompt, ladder, limits). A changed
    criteria list, a new reply of the student, another memory or an edited
    stage is another key — the stored answer is then never read. The JSON is
    ``dataclasses.asdict`` of the input with the keys sorted; a pydantic model
    inside (a criterion) is written by ``model_dump(mode="json")``, not by its
    ``str()``, so the form does not depend on a ``repr``.

The value:
    JSON of :class:`_Kept`: the answer (``EvaluationAnswer``), the ``provider``
    and ``model`` that gave it, the ``call_id`` of its register row, and
    ``stored_at`` (seconds since the epoch, for the age in the log). It is
    read through :func:`~course_supporter.models.stored_read.read_stored`: a
    field a newer release added is left out, anything else wrong is
    "unreadable".

Lifetime:
    ``(submission_path_max_retries + 1) * submission_path_retry_defer_s + 1800``
    seconds (:func:`first_answer_ttl_s`): every pass of the path with its
    deferral, and half an hour for the queue and the passes themselves. A
    failed retry does not touch the key — its lifetime takes it. A pass that
    writes the verdict rows deletes it.

Failure:
    No queue (``None``), an error of Redis on a read, a write or a delete, a
    value that cannot be read: the stage goes as it went without this module,
    and the log gets one warning line with a reason code and no value
    (``criteria_evaluation_first_answer_discarded``).

What the log holds:
    Identifiers, codes and numbers. The value in Redis holds the model's words
    and the student's quotes; the log never does.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
import uuid
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from course_supporter.agents.criteria_evaluator import Evaluated
from course_supporter.config import Settings, get_settings
from course_supporter.homework.criteria_verdicts import EvaluationAnswer
from course_supporter.models.stored_read import problems_without_values, read_stored

if TYPE_CHECKING:
    from arq.connections import ArqRedis

    from course_supporter.agents.criteria_evaluator import EvaluationInput
    from course_supporter.llm.stage_router import StageExecution

KEY_PREFIX: Final = "cs:first_answer"
"""The first part of every key, so the keys can be told from the queue's."""

SAFETY_MARGIN_S: Final = 1800
"""Half an hour on top of the passes and their deferrals."""

type DiscardReason = Literal["unreadable", "rung_gone", "redis_error"]
type _Operation = Literal["get", "set", "delete"]


def first_answer_ttl_s(settings: Settings | None = None) -> int:
    """How many seconds a kept first answer lives.

    ``(submission_path_max_retries + 1) * submission_path_retry_defer_s + 1800``
    — with the settings as they come, ``(2 + 1) * 60 + 1800 = 1980``.
    """
    used = settings if settings is not None else get_settings()
    return (
        used.submission_path_max_retries + 1
    ) * used.submission_path_retry_defer_s + SAFETY_MARGIN_S


def first_answer_digest(shown: EvaluationInput, execution: StageExecution) -> str:
    """What the key says of the input and the stage: a SHA-256, in hex.

    The same input and stage give the same digest in every process: the JSON
    has sorted keys and no value of a type whose text depends on the run.
    """
    body = json.dumps(
        dataclasses.asdict(shown),
        sort_keys=True,
        ensure_ascii=False,
        default=_canonical,
    )
    digest = hashlib.sha256(body.encode())
    digest.update(b"\x00")
    digest.update(execution.stage.model_dump_json().encode())
    return digest.hexdigest()


def first_answer_key(
    submission_id: uuid.UUID,
    stage_name: str,
    shown: EvaluationInput,
    execution: StageExecution,
) -> str:
    """The Redis key of the first answer of one stage over one input."""
    digest = first_answer_digest(shown, execution)
    return f"{KEY_PREFIX}:{submission_id}:{stage_name}:{digest}"


def _canonical(value: object) -> Any:
    """What ``json`` writes for a value it cannot write itself."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return str(value)


class _Kept(BaseModel):
    """The stored value: the first answer and where it came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: EvaluationAnswer
    provider: str
    model: str
    call_id: uuid.UUID | None
    stored_at: float


@dataclasses.dataclass(frozen=True, slots=True)
class Recalled:
    """A first answer read back: the answer as the stage had it, and its age."""

    evaluated: Evaluated
    age_s: int


class FirstAnswerStore:
    """One key of the kept first answers, over a queue that may fail.

    Args:
        redis: The queue's connection (``StageContext.arq``).
        key: :func:`first_answer_key` of this stage's input.
        log: The stage's logger, with the submission and the stage bound.
        ttl_s: The value's lifetime; ``None`` — :func:`first_answer_ttl_s`.
    """

    def __init__(
        self,
        redis: ArqRedis,
        key: str,
        *,
        log: Any,
        ttl_s: int | None = None,
    ) -> None:
        self._redis = redis
        self._key = key
        self._log = log
        self._ttl_s = ttl_s if ttl_s is not None else first_answer_ttl_s()
        self.found = False
        """Something stood under the key when it was read (readable or not)."""

    async def recall(self) -> Recalled | None:
        """The kept first answer, or ``None`` — none, or one that is refused."""
        try:
            raw = await self._redis.get(self._key)
        except Exception:
            self._failed("get")
            return None
        if raw is None:
            return None
        self.found = True
        try:
            kept = read_stored(_Kept, json.loads(raw))
        except (ValueError, TypeError, ValidationError) as error:
            # ``ValueError`` is the JSON's and ``UnicodeDecodeError``'s;
            # pydantic's error is a ``ValueError`` too. The place and the kind
            # of what is wrong, never a value.
            self.discard(
                "unreadable",
                problems=(
                    problems_without_values(error)
                    if isinstance(error, ValidationError)
                    else None
                ),
            )
            return None
        age_s = max(0, int(time.time() - kept.stored_at))
        evaluated = Evaluated(
            answer=kept.answer,
            provider=kept.provider,
            model=kept.model,
            call_id=kept.call_id,
        )
        return Recalled(evaluated, age_s)

    async def keep(self, first: Evaluated, *, items: int) -> None:
        """Put the first answer under the key for the length of the retries."""
        value = _Kept(
            answer=first.answer,
            provider=first.provider,
            model=first.model,
            call_id=first.call_id,
            stored_at=time.time(),
        )
        try:
            await self._redis.set(self._key, value.model_dump_json(), ex=self._ttl_s)
        except Exception:
            self._failed("set")
            return
        self._log.info(
            "criteria_evaluation_first_answer_kept",
            call_id=None if first.call_id is None else str(first.call_id),
            items_to_ask_again=items,
            ttl_s=self._ttl_s,
        )

    async def forget(self) -> None:
        """Delete the key: the verdict rows are written, the answer is spent."""
        try:
            await self._redis.delete(self._key)
        except Exception:
            self._failed("delete")

    def discard(self, reason: DiscardReason, **fields: Any) -> None:
        """Say that a kept answer is not used, and why — a code, no value."""
        self._log.warning(
            "criteria_evaluation_first_answer_discarded", reason=reason, **fields
        )

    def reused(self, recalled: Recalled) -> None:
        """Say that the stage begins at the second request."""
        call_id = recalled.evaluated.call_id
        self._log.info(
            "criteria_evaluation_first_answer_reused",
            call_id=None if call_id is None else str(call_id),
            age_s=recalled.age_s,
        )

    def _failed(self, operation: _Operation) -> None:
        # Not ``exc_info``: a connection error may carry the address, and the
        # line is one fact — Redis failed — not a trace.
        self.discard("redis_error", operation=operation)
