"""The public numbers (BIL-PUBLIC-NUMBERS): what a review costs, from the base.

Purpose:
    The site, the about page, the student's portal and the author's app show
    the same numbers of what a review costs and how far a package goes
    (operator 2026-11-19/20): the backend is their one source, and they come
    from real data. :func:`compute_public_numbers` computes them; the public
    address ``GET /api/v1/public/numbers`` serves them and the portal's
    reminder (:func:`.outlook.student_outlook`) reads its typical P and F from
    them.

    * ``packages`` — the catalog's packages on sale, fewest units first, a line
      per package and currency; ``validity_months`` is the journal's
      :data:`~.journal.VALIDITY_MONTHS`.
    * ``review_cost.homework`` (``task``, ``short_task``) and
      ``review_cost.project`` — over every tenant (the address is public and
      general), the submissions of the last ``public_numbers_window_days``
      days that are ``completed`` or ``delivered``:

      - ``actual`` — the fact F of each review in units
        (:func:`.charging.review_facts_usd` in one query,
        :func:`.charging.fact_of`), facts of 0 (a copy, nothing called) left
        out: the smallest, the median rounded as a charged fact (a half up,
        :func:`~course_supporter.cost_estimate.core.round_half_up`), the
        largest;
      - ``start_threshold`` — the price before P of the estimate of each
        review with a fact,
        today's :func:`.charging.price_of` of its ``e_usd`` (not the stored
        ``p_units``, written at the rate of its day), 0 left out: the median
        rounded up (a threshold is never understated) and the largest;
      - ``smallest_package_reviews`` — :func:`.outlook.reviews_left` of the
        smallest package's units: ``typical`` with the group's typical P and
        F; ``low`` and ``high`` the least and the most of the same number
        computed per task (its median P and F, rounded the same way) that
        has both. When ``low`` and ``high`` are present, ``typical`` is
        clamped to ``[low, high]`` because the group's typical P and F come
        from different tasks and could otherwise fall outside that range.

      Every number is a whole unit, rounded once here: the site, the portal
      and :func:`.outlook.student_outlook` take these very numbers and round
      nothing of their own.

      ``status``: ``ready`` — the sample has a review with a fact;
      ``insufficient_data`` — it has none (numbers null); ``not_charged`` — no
      type of the group is charged today: served by today's Mentor or on its
      path's test mode (``config/submission_paths.yaml``), as the funds port
      and the price of a review read it.
    * ``sample.includes_internal`` — always true: our own (test) students are
      in the sample until there is data enough without them.

    No ids, names or anything about one person. No formula is copied: the
    numbers are those of ``price_of``, ``fact_of``, ``review_facts_usd`` and
    ``reviews_left``.

Once per start of the app:
    The numbers are computed once per start of the app's container and are
    the same in every process of it (uvicorn ``--workers``): the first
    process to compute writes them to Redis under a key of that start
    (:func:`start_key`) with ``SET NX``; the rest read them
    (:class:`PublicNumbersStore`). Between starts they do not change, whatever
    the base gains. A failure stops nothing: the next read tries again.
"""

from __future__ import annotations

import hashlib
import os
import socket
import statistics
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import structlog
from pydantic import TypeAdapter
from sqlalchemy import select

from course_supporter.billing import charging
from course_supporter.billing.journal import VALIDITY_MONTHS
from course_supporter.billing.kinds import ActionKind
from course_supporter.billing.outlook import reviews_left
from course_supporter.billing.payments.catalog import Catalog, get_catalog
from course_supporter.cost_estimate.core import (
    RESERVE_MULTIPLIER,
    round_half_up,
    round_up,
)
from course_supporter.homework.path_config import (
    PathConfig,
    ServedBy,
    get_path_config,
    in_test_mode,
)
from course_supporter.models.source import AssignmentType
from course_supporter.storage.orm import (
    ActionCostEstimate,
    AuthoredDocument,
    HomeworkStatus,
    HomeworkSubmission,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger()

CONTRACT_VERSION: Final = 1
"""The version of the shape; a change of a field's meaning is a new one."""

HOMEWORK_TYPES: Final = (AssignmentType.TASK, AssignmentType.SHORT_TASK)
PROJECT_TYPES: Final = (AssignmentType.PROJECT,)

FINISHED_REVIEWS: Final = (
    HomeworkStatus.COMPLETED.value,
    HomeworkStatus.DELIVERED.value,
)
"""The submission states of a review that ran to its end."""

Status = Literal["ready", "insufficient_data", "not_charged"]


@dataclass(frozen=True, slots=True)
class Rules:
    reserve_multiplier: int


@dataclass(frozen=True, slots=True)
class PackageLine:
    id: str
    units: int
    price_minor: int
    currency: str
    validity_months: int
    tax_mode: Literal["inclusive", "exclusive"] | None


@dataclass(frozen=True, slots=True)
class Actual:
    min: int | None
    typical: int | None
    max: int | None


@dataclass(frozen=True, slots=True)
class StartThreshold:
    typical: int | None
    max: int | None


@dataclass(frozen=True, slots=True)
class SmallestPackageReviews:
    low: int | None
    typical: int | None
    high: int | None


@dataclass(frozen=True, slots=True)
class Sample:
    reviews: int
    window_days: int
    includes_internal: bool


@dataclass(frozen=True, slots=True)
class ReviewGroup:
    status: Status
    actual: Actual
    start_threshold: StartThreshold
    smallest_package_reviews: SmallestPackageReviews
    sample: Sample


@dataclass(frozen=True, slots=True)
class ReviewCost:
    homework: ReviewGroup
    project: ReviewGroup


@dataclass(frozen=True, slots=True)
class PublicNumbers:
    """The numbers of one start (module docstring); the address's shape."""

    contract_version: int
    as_of: datetime
    rules: Rules
    packages: tuple[PackageLine, ...]
    review_cost: ReviewCost


_ADAPTER: Final = TypeAdapter(PublicNumbers)


# ── The computation ────────────────────────────────────────────────────


def _charged(task_type: AssignmentType, config: PathConfig) -> bool:
    """Does a review of ``task_type`` charge units today?

    Today's Mentor charges nothing (a type the file does not declare stays
    with it), nor does the new path on its test mode.
    """
    declared = config.task_types.get(task_type)
    return (
        declared is not None
        and declared.served_by is ServedBy.NEW_PATH
        and not in_test_mode(task_type, config)
    )


def _packages(catalog: Catalog) -> tuple[PackageLine, ...]:
    return tuple(
        PackageLine(
            id=package.id,
            units=package.units,
            price_minor=price,
            currency=currency,
            validity_months=VALIDITY_MONTHS,
            tax_mode=package.tax_mode,
        )
        for package in sorted(catalog.active(), key=lambda p: p.units)
        for currency, price in sorted(
            package.prices_minor.items(), key=lambda item: (item[0] != "EUR", item[0])
        )
    )


def _median(values: Sequence[int]) -> Decimal:
    """The exact median: of an even count, the half of the two middle ones."""
    return Decimal(statistics.median(map(Decimal, values)))


def _typical_price(prices: Sequence[int]) -> int:
    """The median P up to a whole unit: a threshold is never understated."""
    return round_up(_median(prices))


def _typical_fact(facts: Sequence[int]) -> int:
    """The median F rounded as a charged fact is (a half up)."""
    return round_half_up(_median(facts))


def _smallest_package_reviews(
    smallest_units: int,
    typical_p: int,
    typical_f: int,
    per_task: Sequence[int],
) -> SmallestPackageReviews:
    """Range and clamped typical reviews for the smallest package."""
    low = min(per_task, default=None)
    high = max(per_task, default=None)
    typical = reviews_left(smallest_units, typical_p, typical_f)
    if low is not None and high is not None:
        # The group's typical P and F come from different tasks, so without
        # clamping typical can fall outside [low, high].
        typical = min(max(typical, low), high)
    return SmallestPackageReviews(low=low, typical=typical, high=high)


def _empty_group(status: Status, window_days: int) -> ReviewGroup:
    """A group without numbers: none in the sample, or not charged."""
    return ReviewGroup(
        status=status,
        actual=Actual(None, None, None),
        start_threshold=StartThreshold(None, None),
        smallest_package_reviews=SmallestPackageReviews(None, None, None),
        sample=Sample(0, window_days, includes_internal=True),
    )


async def _group(
    session: AsyncSession,
    types: Sequence[AssignmentType],
    *,
    since: datetime,
    window_days: int,
    smallest_units: int | None,
    tenant_ids: Collection[uuid.UUID] | None,
) -> ReviewGroup:
    """The numbers of the finished reviews of ``types`` since ``since``."""
    query = (
        select(HomeworkSubmission.id, HomeworkSubmission.authored_document_id)
        .join(
            AuthoredDocument,
            AuthoredDocument.id == HomeworkSubmission.authored_document_id,
        )
        .where(
            HomeworkSubmission.status.in_(FINISHED_REVIEWS),
            HomeworkSubmission.created_at >= since,
            AuthoredDocument.task_type.in_([t.value for t in types]),
        )
    )
    if tenant_ids is not None:
        query = query.where(HomeworkSubmission.tenant_id.in_(tenant_ids))
    task_of = dict((await session.execute(query)).tuples().all())
    usd = await charging.review_facts_usd(session, list(task_of))
    facts = {sid: f for sid, u in usd.items() if (f := charging.fact_of(u)) > 0}
    if not facts:
        return _empty_group("insufficient_data", window_days)

    estimates = await session.execute(
        select(ActionCostEstimate.submission_id, ActionCostEstimate.e_usd).where(
            ActionCostEstimate.action_kind == ActionKind.REVIEW.value,
            ActionCostEstimate.submission_id.in_(list(facts)),
        )
    )
    prices = {
        sid: p
        for sid, e in estimates.tuples()
        if sid is not None and (p := charging.price_of(e)) > 0
    }

    typical_f = _typical_fact(list(facts.values()))
    typical_p = _typical_price(list(prices.values())) if prices else None
    per_task_f: dict[uuid.UUID, list[int]] = defaultdict(list)
    per_task_p: dict[uuid.UUID, list[int]] = defaultdict(list)
    for sid, fact in facts.items():
        per_task_f[task_of[sid]].append(fact)
    for sid, price in prices.items():
        per_task_p[task_of[sid]].append(price)

    left = SmallestPackageReviews(None, None, None)
    if smallest_units is not None and typical_p is not None:
        per_task = [
            reviews_left(
                smallest_units,
                _typical_price(per_task_p[t]),
                _typical_fact(per_task_f[t]),
            )
            for t in per_task_f
            if t in per_task_p
        ]
        left = _smallest_package_reviews(smallest_units, typical_p, typical_f, per_task)
    return ReviewGroup(
        status="ready",
        actual=Actual(min(facts.values()), typical_f, max(facts.values())),
        start_threshold=StartThreshold(
            typical_p, max(prices.values()) if prices else None
        ),
        smallest_package_reviews=left,
        sample=Sample(len(facts), window_days, includes_internal=True),
    )


async def compute_public_numbers(
    session: AsyncSession,
    *,
    window_days: int,
    now: datetime | None = None,
    catalog: Catalog | None = None,
    path_config: PathConfig | None = None,
    tenant_ids: Collection[uuid.UUID] | None = None,
) -> PublicNumbers:
    """The numbers as of ``now`` (module docstring). Reads only.

    ``tenant_ids`` — None for every tenant (the public address); a test
    names its own world, since the test base is shared.
    """
    now = now or datetime.now(UTC)
    catalog = catalog if catalog is not None else get_catalog()
    config = path_config if path_config is not None else get_path_config()
    packages = _packages(catalog)
    smallest = packages[0].units if packages else None
    since = now - timedelta(days=window_days)

    async def group(types: Sequence[AssignmentType]) -> ReviewGroup:
        charged = [t for t in types if _charged(t, config)]
        if not charged:
            return _empty_group("not_charged", window_days)
        return await _group(
            session,
            charged,
            since=since,
            window_days=window_days,
            smallest_units=smallest,
            tenant_ids=tenant_ids,
        )

    return PublicNumbers(
        contract_version=CONTRACT_VERSION,
        as_of=now,
        rules=Rules(reserve_multiplier=RESERVE_MULTIPLIER),
        packages=packages,
        review_cost=ReviewCost(
            homework=await group(HOMEWORK_TYPES),
            project=await group(PROJECT_TYPES),
        ),
    )


# ── One answer per start ───────────────────────────────────────────────


KEY_PREFIX: Final = "public_numbers:v1"
KEY_TTL_SECONDS: Final = 7 * 24 * 3600
"""A start's key lives this long after its last read: the keys of starts
long gone leave Redis by themselves."""


def start_key() -> str:
    """The Redis key of this start of the container.

    ``hostname`` (Docker gives a container its id: a new one on every
    re-creation), the host's boot id and the start time of process 1 (the
    uvicorn parent in the app's image: a restart of the container starts it
    anew). Every worker of the parent, a respawned one too, reads the same
    three. Where ``/proc`` cannot tell (not Linux), the parent's pid stands
    in. Outside a container process 1 is the host's: the key then holds until
    the host reboots or the key expires.
    """
    host = socket.gethostname()
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        stat = Path("/proc/1/stat").read_text()
        # Field 22, starttime; the name in field 2 may hold spaces, so count
        # from the ")" that closes it (field 3 is the first after it).
        started = stat.rpartition(")")[2].split()[19]
    except (OSError, IndexError):
        boot, started = "-", f"ppid{os.getppid()}"
    return f"{KEY_PREFIX}:{host}:{boot}:{started}"


class RedisLike(Protocol):
    """What the store needs of Redis (redis-py's asyncio client)."""

    def getex(self, name: str, *, ex: int) -> Awaitable[Any]: ...

    def set(self, name: str, value: bytes, *, ex: int, nx: bool) -> Awaitable[Any]: ...


@dataclass(frozen=True, slots=True)
class Published:
    """The numbers of this start and the very bytes every process serves."""

    numbers: PublicNumbers
    body: bytes
    etag: str


def published(body: bytes) -> Published:
    """The numbers in ``body`` and its ETag (a hash of the bytes)."""
    return Published(
        numbers=_ADAPTER.validate_json(body),
        body=body,
        etag=f'"{hashlib.sha256(body).hexdigest()[:32]}"',
    )


def dump(numbers: PublicNumbers) -> bytes:
    """The JSON of ``numbers``: the body of the address."""
    return _ADAPTER.dump_json(numbers)


class PublicNumbersStore:
    """The numbers of one start, shared by its processes through Redis.

    Every read slides the key's life (``GETEX``), so a key outlives its start
    by :data:`KEY_TTL_SECONDS` at most and never expires under a running one.
    """

    def __init__(
        self,
        redis: RedisLike,
        key: str,
        compute: Callable[[], Awaitable[PublicNumbers]],
    ) -> None:
        self._redis = redis
        self._key = key
        self._compute = compute

    async def get(self) -> Published | None:
        """The numbers of this start; computed and written by the first
        process that finds none. None when that failed — the next call
        tries again."""
        try:
            body: bytes | None = await self._redis.getex(self._key, ex=KEY_TTL_SECONDS)
            if body is None:
                fresh = dump(await self._compute())
                await self._redis.set(self._key, fresh, ex=KEY_TTL_SECONDS, nx=True)
                # Another process may have written first: serve what it wrote.
                body = await self._redis.getex(self._key, ex=KEY_TTL_SECONDS)
            return published(body) if body is not None else None
        except Exception:
            logger.warning("public_numbers_unavailable", key=self._key, exc_info=True)
            return None


def compute_with(
    session_factory: async_sessionmaker[AsyncSession], *, window_days: int
) -> Callable[[], Awaitable[PublicNumbers]]:
    """:func:`compute_public_numbers` over every tenant, in a session of its own."""

    async def compute() -> PublicNumbers:
        async with session_factory() as session:
            return await compute_public_numbers(session, window_days=window_days)

    return compute
