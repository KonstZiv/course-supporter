"""The student's replies to the Mentor's remarks, as the reviewing stages see them.

Purpose:
    Task 12a, K4. Between two submissions the student may reply under a remark
    of the review: ask, object, comment. On the next submission — the path
    ``repeat_with_replies`` — the evaluation sees the replies, and the
    explanation answers each of them. Only the WORK is graded (the product
    owner's principle, 2026-11-16): a reply is the student's claim, never
    evidence; it may point the evaluation to a place in the work, and a verdict
    that changes still stands on a quote of the student's files (the quote rule
    of :mod:`course_supporter.homework.criteria_verdicts`, unchanged). This
    module decides, by code alone and with no session, what each model is shown
    and how each reply is answered.

Interface:
    :class:`ThreadEntry` — one comment of a remark's thread, as read: the
    student's reply or the Mentor's earlier answer.
    :class:`EvaluationReply`, :func:`replies_for_evaluation` — what the
    evaluation is shown.
    :class:`AnswerWay`, :class:`AskedReply`, :class:`RepliesView`,
    :class:`RepliesPlan`, :func:`plan_replies` — what the explanation is
    shown, and how each reply without an answer is answered.

What each model sees:
    * The evaluation — the student's replies without an answer, on the topic
      and not hostile, each with its item, kind and text. Never a hostile
      reply, never one off the topic, never the Mentor's answers.
    * The explanation — every thread (one per item), in the order of time,
      each entry with the local id of the entry it answers ("in reply to").
      A hostile reply is shown by its category alone, NEVER by its text; a
      reply answered by the phrasebook alone (below) is not shown at all.

How a reply without an answer is answered (:class:`AnswerWay`):
    * on the topic — the model answers it;
    * off the topic — the code counts the thread's rounds off the topic: every
      reply of the student marked ``off_topic`` in the thread, answered or
      not, up to and including this one. Rounds 1 and 2: the model answers
      briefly and brings the conversation back to the work; round 3: the
      model folds the conversation (there is work; the time is better spent on
      the course); from round 4: the phrasebook's answer, no model;
    * hostile — the model writes a short friendly joke about the attempt in
      a style the code chose (:mod:`course_supporter.homework.reply_humor`),
      shown only the category, the item and the style; the code checks the
      joke and replaces one that fails with the phrasebook's
      (:mod:`course_supporter.homework.verdict_explanation`). A hostile reply
      is not a round off the topic.

The budget:
    Input is bounded twice. Every text shown — a reply, an earlier answer —
    is cut at :data:`MAX_SHOWN_TEXT_CHARS` characters, marked as cut. The
    threads are cut at :data:`THREADS_BUDGET_CHARS`, the evaluation's replies
    at :data:`EVALUATION_BUDGET_CHARS`: when they do not fit, the OLDEST
    entries go first and the newest stay, and a reply the model must answer
    is never cut away. The sizes are counted as text plus a fixed overhead an
    entry's other fields take in the prompt's JSON — an estimate, not a
    measurement: the budget keeps a request in bounds, it is not a ceiling.

Replacing:
    The stages depend on :func:`replies_for_evaluation` and
    :func:`plan_replies` and on the value types; where the threads come from
    and where the answers go is :mod:`course_supporter.homework.reply_source`.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from course_supporter.homework.reply_humor import HumorStyle, choose_style

STUDENT_REPLY_KINDS: Final = frozenset({"question", "objection", "comment"})
"""What a student's reply may be — the kinds the review names apart
(``models/review_structure.ReplyKind``)."""

MENTOR_ANSWER_KIND: Final = "answer"
"""The kind of the Mentor's answer in a thread."""

WRAP_UP_ROUND: Final = 3
"""The round off the topic on which the model folds the conversation."""

CANNED_FROM_ROUND: Final = 4
"""From this round off the topic on, the phrasebook answers, never the model."""

MAX_SHOWN_TEXT_CHARS: Final = 4_000
"""Characters of one shown text at most; a longer one is cut (see the module)."""

THREADS_BUDGET_CHARS: Final = 24_000
"""The explanation's threads, in characters (see "The budget")."""

EVALUATION_BUDGET_CHARS: Final = 12_000
"""The evaluation's replies, in characters (see "The budget")."""

ENTRY_OVERHEAD_CHARS: Final = 120
"""What an entry's fields besides its text take in the prompt's JSON."""

CUT_MARK: Final = " […]"
"""What ends a text cut at :data:`MAX_SHOWN_TEXT_CHARS`."""

UNKNOWN_CATEGORY: Final = "unknown"
"""A hostile reply's category when the one stored is not a plain identifier."""

# A hostile category is our own word, set by the safety stage — never the
# student's text. Anything else in its place is not shown.
_CATEGORY = re.compile(r"[a-z][a-z0-9_]{0,63}")
_DIGITS = re.compile(r"(\d+)")


class ThreadAuthor(StrEnum):
    """Who wrote an entry of a thread."""

    STUDENT = "student"
    MENTOR = "mentor"


class ReplyTopic(StrEnum):
    """What the safety stage made of a student's reply (task 12a, K3)."""

    ON_TOPIC = "on_topic"
    OFF_TOPIC = "off_topic"
    HOSTILE = "hostile"


class AnswerWay(StrEnum):
    """How a reply without an answer is answered (see the module)."""

    ANSWER = "answer"
    WRAP_UP = "wrap_up"
    JOKE = "joke"
    CANNED = "canned"


@dataclass(frozen=True, slots=True)
class ThreadEntry:
    """One comment of a remark's thread, as the stages read it.

    Attributes:
        comment_id: The comment's stored id, as text.
        item_id: The item the thread is under: a criterion (``c3``) or one of
            its points (``c3.p2``).
        author: The student or the Mentor.
        kind: A student's ``question``, ``objection`` or ``comment``; the
            Mentor's ``answer``.
        text: What was written.
        created_at: When; the thread's order.
        parent_id: The comment this one answers, if any.
        topic: A student's reply as the safety stage marked it; ``None`` for
            the Mentor's.
        hostile_category: What a hostile reply attempted, as the safety stage
            named it.
        answered: A student's reply already answered by an earlier review.

    Raises:
        ValueError: A student's entry without a topic or of a kind the review
            does not name, or a Mentor's entry that is not an answer or has a
            topic.
    """

    comment_id: str
    item_id: str
    author: ThreadAuthor
    kind: str
    text: str
    created_at: datetime
    parent_id: str | None = None
    topic: ReplyTopic | None = None
    hostile_category: str | None = None
    answered: bool = False

    def __post_init__(self) -> None:
        if self.author is ThreadAuthor.STUDENT:
            if self.kind not in STUDENT_REPLY_KINDS:
                msg = f"reply {self.comment_id}: kind {self.kind!r} is not a reply's"
                raise ValueError(msg)
            if self.topic is None:
                msg = f"reply {self.comment_id} has no topic"
                raise ValueError(msg)
        elif self.kind != MENTOR_ANSWER_KIND or self.topic is not None:
            msg = f"answer {self.comment_id}: kind {self.kind!r}, topic {self.topic}"
            raise ValueError(msg)

    @property
    def awaits_answer(self) -> bool:
        """A student's reply no review has answered yet."""
        return self.author is ThreadAuthor.STUDENT and not self.answered


# ── What the evaluation is shown ──


@dataclass(frozen=True, slots=True)
class EvaluationReply:
    """One reply, as the evaluation is shown it: an item, a kind, a text."""

    item_id: str
    kind: str
    text: str


def replies_for_evaluation(
    entries: Iterable[ThreadEntry],
    *,
    budget_chars: int = EVALUATION_BUDGET_CHARS,
    max_text_chars: int = MAX_SHOWN_TEXT_CHARS,
) -> tuple[EvaluationReply, ...]:
    """The student's replies the evaluation is shown, in the order of time.

    Only those without an answer, on the topic — never a hostile one, never
    one off the topic, never the Mentor's answers — and of those the newest
    that fit ``budget_chars``.
    """
    candidates = sorted(
        (e for e in entries if e.awaits_answer and e.topic is ReplyTopic.ON_TOPIC),
        key=_time_order,
    )
    kept: list[EvaluationReply] = []
    used = 0
    for entry in reversed(candidates):
        shown = EvaluationReply(
            entry.item_id, entry.kind, shown_text(entry.text, max_text_chars)
        )
        cost = len(shown.text) + ENTRY_OVERHEAD_CHARS
        if used + cost > budget_chars:
            break
        kept.append(shown)
        used += cost
    return tuple(reversed(kept))


# ── What the explanation is shown and asked ──


@dataclass(frozen=True, slots=True)
class ShownEntry:
    """One entry of a thread, as the explanation is shown it.

    ``text`` is ``None`` for a hostile reply, which is shown by ``hostile``
    — its category — alone. ``in_reply_to`` is the local id of the entry
    this one answers; that entry may be older than the budget kept.
    """

    id: str
    author: ThreadAuthor
    kind: str
    in_reply_to: str | None
    topic: ReplyTopic | None = None
    text: str | None = None
    hostile: str | None = None


@dataclass(frozen=True, slots=True)
class ShownThread:
    """The thread under one item, in the order of time."""

    item: str
    entries: tuple[ShownEntry, ...]


@dataclass(frozen=True, slots=True)
class ShownAsk:
    """A reply the model answers now, and how (never :attr:`AnswerWay.CANNED`).

    Attributes:
        id: The reply's local id in the threads.
        how: Answer it, fold the conversation, or joke about the attempt.
        item: The item it is under.
        off_topic_round: Its round off the topic, for a reply off the topic.
        course: The course's name, which a folded conversation points to.
        hostile: A hostile reply's category.
        style: The style of the joke, chosen by code.
        work_only: The sentence a joke must carry word for word: only the
            work is graded (the phrasebook's ``reply.work_only``).
    """

    id: str
    how: AnswerWay
    item: str
    off_topic_round: int | None = None
    course: str | None = None
    hostile: str | None = None
    style: HumorStyle | None = None
    work_only: str | None = None


@dataclass(frozen=True, slots=True)
class RepliesView:
    """Everything the explanation is shown of the replies."""

    threads: tuple[ShownThread, ...]
    to_answer: tuple[ShownAsk, ...]


@dataclass(frozen=True, slots=True)
class AskedReply:
    """A reply without an answer, and how it is answered.

    Attributes:
        local_id: Its id in the threads the model is shown.
        entry: The reply as read — with its text, which a hostile reply's
            check needs and the model never sees.
        how: See :class:`AnswerWay`.
        off_topic_round: Its round off the topic, when off the topic.
        style: The joke's style, for a hostile reply.
    """

    local_id: str
    entry: ThreadEntry
    how: AnswerWay
    off_topic_round: int | None = None
    style: HumorStyle | None = None


@dataclass(frozen=True, slots=True)
class RepliesPlan:
    """What the explanation is shown, and every reply it is to answer."""

    view: RepliesView
    asked: tuple[AskedReply, ...]

    @property
    def to_answer(self) -> tuple[str, ...]:
        """The local ids the model must answer, each once (its answer's check)."""
        return tuple(ask.id for ask in self.view.to_answer)


def plan_replies(
    entries: Iterable[ThreadEntry],
    *,
    styles: Sequence[HumorStyle],
    course: str,
    work_only: str,
    budget_chars: int = THREADS_BUDGET_CHARS,
    max_text_chars: int = MAX_SHOWN_TEXT_CHARS,
    style_for: Callable[[str], HumorStyle] | None = None,
) -> RepliesPlan:
    """What the explanation is shown of the threads, and how each reply is answered.

    Args:
        entries: Every entry of every thread under the remarks of this task.
        styles: The joke's styles, in the file's order
            (:func:`~course_supporter.homework.reply_humor.get_humor_styles`).
        course: The course's name — what a folded conversation points to.
        work_only: The sentence a joke carries word for word, in the review's
            language.
        budget_chars: The threads' budget (see the module).
        max_text_chars: One text's budget (see the module).
        style_for: Another choice of a joke's style than by the reply's id —
            the measurement's sweep over every style; ``None`` in production.

    Raises:
        ValueError: Two entries share a comment id.
    """
    threads = _threads(entries)
    local = _local_ids(threads)
    asked: list[AskedReply] = []
    for thread in threads.values():
        asked.extend(_asked_in(thread, local, styles=styles, style_for=style_for))
    asked.sort(key=lambda ask: _time_order(ask.entry))

    canned = {a.entry.comment_id for a in asked if a.how is AnswerWay.CANNED}
    keep = {a.entry.comment_id for a in asked if a.how is not AnswerWay.CANNED}
    shown = _within_budget(
        [
            e
            for thread in threads.values()
            for e in thread
            if e.comment_id not in canned
        ],
        keep=keep,
        budget_chars=budget_chars,
        max_text_chars=max_text_chars,
    )
    view = RepliesView(
        threads=tuple(
            ShownThread(
                item=item,
                entries=tuple(
                    _shown(e, local, max_text_chars)
                    for e in thread
                    if e.comment_id in shown
                ),
            )
            for item, thread in threads.items()
            if any(e.comment_id in shown for e in thread)
        ),
        to_answer=tuple(
            _ask(a, course=course, work_only=work_only)
            for a in asked
            if a.how is not AnswerWay.CANNED
        ),
    )
    return RepliesPlan(view=view, asked=tuple(asked))


def shown_text(text: str, max_chars: int = MAX_SHOWN_TEXT_CHARS) -> str:
    """``text`` as a prompt shows it: cut at ``max_chars``, marked as cut."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + CUT_MARK


def hostile_category(raw: str | None) -> str:
    """A hostile reply's category as shown: our own identifier, or ``unknown``.

    The category is the safety stage's word; anything that is not a plain
    identifier is not shown, whatever it holds.
    """
    if raw is not None and _CATEGORY.fullmatch(raw):
        return raw
    return UNKNOWN_CATEGORY


def _time_order(entry: ThreadEntry) -> tuple[datetime, str]:
    return entry.created_at, entry.comment_id


def _item_order(item_id: str) -> tuple[tuple[int, int | str], ...]:
    """``c2`` before ``c10``, ``c2`` before ``c2.p1``: the list's own order."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in _DIGITS.split(item_id)
        if part
    )


def _threads(entries: Iterable[ThreadEntry]) -> dict[str, list[ThreadEntry]]:
    """The entries by item, items in the list's order, each in the order of time."""
    listed = list(entries)
    repeated = sorted(
        cid for cid, n in Counter(e.comment_id for e in listed).items() if n > 1
    )
    if repeated:
        msg = f"comments repeat: {repeated}"
        raise ValueError(msg)
    by_item: dict[str, list[ThreadEntry]] = defaultdict(list)
    for entry in listed:
        by_item[entry.item_id].append(entry)
    return {
        item: sorted(by_item[item], key=_time_order)
        for item in sorted(by_item, key=_item_order)
    }


def _local_ids(threads: dict[str, list[ThreadEntry]]) -> dict[str, str]:
    """``r1``, ``r2``… over the threads in their order — one request's ids."""
    ordered = [entry for thread in threads.values() for entry in thread]
    return {entry.comment_id: f"r{n}" for n, entry in enumerate(ordered, start=1)}


def _asked_in(
    thread: Sequence[ThreadEntry],
    local: dict[str, str],
    *,
    styles: Sequence[HumorStyle],
    style_for: Callable[[str], HumorStyle] | None,
) -> list[AskedReply]:
    """The thread's replies without an answer, each with its way."""
    asked: list[AskedReply] = []
    rounds = 0
    for entry in thread:
        if entry.author is not ThreadAuthor.STUDENT:
            continue
        if entry.topic is ReplyTopic.OFF_TOPIC:
            rounds += 1
        if entry.answered:
            continue
        local_id = local[entry.comment_id]
        if entry.topic is ReplyTopic.HOSTILE:
            style = (
                style_for(entry.comment_id)
                if style_for is not None
                else choose_style(entry.comment_id, styles)
            )
            asked.append(AskedReply(local_id, entry, AnswerWay.JOKE, style=style))
        elif entry.topic is ReplyTopic.OFF_TOPIC:
            asked.append(AskedReply(local_id, entry, _off_topic_way(rounds), rounds))
        else:
            asked.append(AskedReply(local_id, entry, AnswerWay.ANSWER))
    return asked


def _off_topic_way(round_number: int) -> AnswerWay:
    if round_number >= CANNED_FROM_ROUND:
        return AnswerWay.CANNED
    if round_number == WRAP_UP_ROUND:
        return AnswerWay.WRAP_UP
    return AnswerWay.ANSWER


def _cost(entry: ThreadEntry, max_text_chars: int) -> int:
    """What an entry takes of the budget; a hostile reply's text is not shown."""
    if entry.topic is ReplyTopic.HOSTILE:
        return ENTRY_OVERHEAD_CHARS
    return len(shown_text(entry.text, max_text_chars)) + ENTRY_OVERHEAD_CHARS


def _within_budget(
    entries: Sequence[ThreadEntry],
    *,
    keep: set[str],
    budget_chars: int,
    max_text_chars: int,
) -> set[str]:
    """The ids of the entries shown: the oldest go first, ``keep`` never."""
    total = sum(_cost(e, max_text_chars) for e in entries)
    shown = {e.comment_id for e in entries}
    for entry in sorted(entries, key=_time_order):
        if total <= budget_chars:
            break
        if entry.comment_id in keep:
            continue
        shown.discard(entry.comment_id)
        total -= _cost(entry, max_text_chars)
    return shown


def _shown(
    entry: ThreadEntry, local: dict[str, str], max_text_chars: int
) -> ShownEntry:
    """One entry as shown: a hostile reply by its category, never its text."""
    in_reply_to = local.get(entry.parent_id) if entry.parent_id is not None else None
    if entry.topic is ReplyTopic.HOSTILE:
        return ShownEntry(
            id=local[entry.comment_id],
            author=entry.author,
            kind=entry.kind,
            in_reply_to=in_reply_to,
            topic=entry.topic,
            hostile=hostile_category(entry.hostile_category),
        )
    return ShownEntry(
        id=local[entry.comment_id],
        author=entry.author,
        kind=entry.kind,
        in_reply_to=in_reply_to,
        topic=entry.topic,
        text=shown_text(entry.text, max_text_chars),
    )


def _ask(asked: AskedReply, *, course: str, work_only: str) -> ShownAsk:
    """What the model is told about one reply it answers."""
    entry = asked.entry
    if asked.how is AnswerWay.JOKE:
        return ShownAsk(
            id=asked.local_id,
            how=asked.how,
            item=entry.item_id,
            hostile=hostile_category(entry.hostile_category),
            style=asked.style,
            work_only=work_only,
        )
    if asked.how is AnswerWay.WRAP_UP:
        return ShownAsk(
            id=asked.local_id,
            how=asked.how,
            item=entry.item_id,
            off_topic_round=asked.off_topic_round,
            course=course,
        )
    return ShownAsk(
        id=asked.local_id,
        how=asked.how,
        item=entry.item_id,
        off_topic_round=asked.off_topic_round,
    )
