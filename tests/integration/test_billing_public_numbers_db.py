"""The public numbers (BIL-PUBLIC-NUMBERS), on a live database.

Each test computes the numbers of its own world (the test base is shared).
Numbers: K = 900, margin 0.20 (``config/cost_estimate.yaml``): E = $0.25 is
P = ceil(0.25 x 1.2 x 900) = 270; a call of $0.10 is F = 90. The smallest
package on sale is 500 units.

Requires ``docker compose up -d``; run with ``--run-db`` (and ``--run-redis``
for the store).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from arq.connections import ArqRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.routes.public_numbers import current_numbers
from course_supporter.billing.public_numbers import (
    PublicNumbers,
    PublicNumbersStore,
    compute_public_numbers,
)
from course_supporter.call_outcome import RequestKind
from course_supporter.homework.path_config import (
    PathConfig,
    ServedBy,
    load_path_config,
)
from course_supporter.models.source import AssignmentType
from course_supporter.storage.orm import HomeworkSubmission
from tests._helpers.public_numbers import WINDOW_DAYS, serve_numbers_of
from tests.integration import test_billing_charging_db as bil
from tests.integration.test_billing_outlook_db import (
    _drop_billing,  # noqa: F401 - the fixture drops the worlds' billing rows
    estimate,
    review,
    task,
)

pytestmark = pytest.mark.requires_db

FIRST = RequestKind.FIRST
SHIPPED_PATHS = Path(__file__).resolve().parents[2] / "config" / "submission_paths.yaml"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


async def numbers(
    session_factory: async_sessionmaker[AsyncSession],
    w: bil.World,
    *,
    path_config: PathConfig | None = None,
) -> PublicNumbers:
    async with session_factory() as session:
        return await compute_public_numbers(
            session,
            window_days=WINDOW_DAYS,
            tenant_ids=[w.tenant_id],
            path_config=path_config,
        )


def paths_with(project: dict[str, Any] | None = None, **homework: Any) -> PathConfig:
    """The shipped path file with ``project`` and the two homework types changed."""
    raw = load_path_config(SHIPPED_PATHS).model_dump(mode="json")
    if project is not None:
        raw["task_types"]["project"].update(project)
    for name in ("task", "short_task"):
        raw["task_types"][name].update(homework)
    return PathConfig.model_validate(raw)


class TestTheNumbers:
    async def test_the_numbers_of_the_sample_by_hand(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """In: three finished reviews of a task and a short task. Out: a copy
        (fact 0), a test, a failed review, one submitted 100 days ago."""
        w = await bil.make_world(session_factory)
        short = await task(session_factory, w, "short_task")
        test = await task(session_factory, w, "test")
        await review(session_factory, w, [("0.10", FIRST)], e_usd="0.25")  # 90, 270
        await review(
            session_factory, w, [("0.05", FIRST)], status="delivered", e_usd="0.2"
        )  # 45, 216
        await review(
            session_factory, w, [("0.15", FIRST)], task_id=short, e_usd="0.3"
        )  # 135, 324
        await review(session_factory, w, [], e_usd="1.0")
        await review(session_factory, w, [("0.50", FIRST)], task_id=test, e_usd="1.0")
        await review(session_factory, w, [("0.50", FIRST)], status="failed", e_usd="1")
        old = await review(session_factory, w, [("0.50", FIRST)], e_usd="1.0")
        async with session_factory() as session:
            await session.execute(
                update(HomeworkSubmission)
                .where(HomeworkSubmission.id == old)
                .values(created_at=datetime.now(UTC) - timedelta(days=100))
            )
            await session.commit()

        found = (await numbers(session_factory, w)).review_cost.homework

        assert found.status == "ready"
        assert (found.actual.min, found.actual.typical, found.actual.max) == (
            45,
            90,
            135,
        )
        assert (found.start_threshold.typical, found.start_threshold.max) == (270, 324)
        left = found.smallest_package_reviews
        # typical: 1 + (500 - 270) // 90; task: P 243, F 67.5 -> 1 + 257 // 67.5;
        # short task: P 324, F 135 -> 1 + 176 // 135
        assert (left.low, left.typical, left.high) == (2, 3, 4)
        assert (found.sample.reviews, found.sample.window_days) == (3, WINDOW_DAYS)
        assert found.sample.includes_internal is True

    async def test_the_price_is_todays_price_of_e_not_the_stored_p(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Written at 1 700 units a dollar: stored P = 510; today's is 270."""
        w = await bil.make_world(session_factory)
        done = await review(session_factory, w, [("0.10", FIRST)])
        await estimate(session_factory, w, done, "0.25", p_units=510)

        found = (await numbers(session_factory, w)).review_cost.homework

        assert found.start_threshold.typical == found.start_threshold.max == 270

    async def test_an_even_count_gives_whole_numbers(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """P 216 and 217: the median 216.5 up to 217; F 45 and 108: the median
        76.5 a half up to 77. The package's number is of those very two."""
        w = await bil.make_world(session_factory)
        await review(session_factory, w, [("0.05", FIRST)], e_usd="0.2")  # 45, 216
        await review(session_factory, w, [("0.12", FIRST)], e_usd="0.2005")  # 108, 217

        found = (await numbers(session_factory, w)).review_cost.homework

        assert (found.start_threshold.typical, found.actual.typical) == (217, 77)
        assert isinstance(found.start_threshold.typical, int)
        assert isinstance(found.actual.typical, int)
        left = found.smallest_package_reviews
        # 1 + (500 - 217) // 77, the one task's the same
        assert (left.low, left.typical, left.high) == (4, 4, 4)

    async def test_two_tasks_with_different_p_and_f_give_different_low_and_high(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await bil.make_world(session_factory)
        dear = await task(session_factory, w, "task")
        await review(session_factory, w, [("0.05", FIRST)], e_usd="0.1")  # 45, 108
        await review(
            session_factory, w, [("0.30", FIRST)], task_id=dear, e_usd="0.4"
        )  # 270, 432

        homework = (await numbers(session_factory, w)).review_cost.homework
        found = homework.smallest_package_reviews

        # dear: 1 + 68 // 270; the other: 1 + 392 // 45; typical: P 270, F 157.5
        assert (found.low, found.typical, found.high) == (1, 2, 9)

    async def test_an_empty_world_has_no_numbers_and_the_project_is_not_charged(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await bil.make_world(session_factory)

        found = await numbers(session_factory, w)

        declared = load_path_config(SHIPPED_PATHS).task_types[AssignmentType.PROJECT]
        # Project chain K8: the project is on the new path in test mode —
        # served by it, not charged for — so its numbers are "not_charged".
        assert declared.served_by is ServedBy.NEW_PATH
        assert declared.test_mode
        for group, status in (
            (found.review_cost.homework, "insufficient_data"),
            (found.review_cost.project, "not_charged"),
        ):
            assert group.status == status
            assert (group.actual.min, group.actual.typical, group.actual.max) == (
                None,
                None,
                None,
            )
            assert (group.start_threshold.typical, group.start_threshold.max) == (
                None,
                None,
            )
            left = group.smallest_package_reviews
            assert (left.low, left.typical, left.high) == (None, None, None)
            assert group.sample.reviews == 0

    async def test_a_project_on_the_new_path_is_counted_as_homework(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await bil.make_world(session_factory)
        project = await task(session_factory, w, "project")
        await review(session_factory, w, [("0.10", FIRST)], e_usd="0.25")
        await review(session_factory, w, [("0.20", FIRST)], task_id=project, e_usd="1")
        config = paths_with(
            project={"served_by": ServedBy.NEW_PATH.value, "test_mode": False}
        )

        found = await numbers(session_factory, w, path_config=config)

        project_cost = found.review_cost.project
        assert project_cost.status == "ready"
        assert (project_cost.actual.typical, project_cost.start_threshold.typical) == (
            180,
            1080,
        )
        assert found.review_cost.homework.actual.typical == 90

    async def test_homework_on_the_test_mode_is_not_charged(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await bil.make_world(session_factory)
        await review(session_factory, w, [("0.10", FIRST)], e_usd="0.25")

        found = await numbers(
            session_factory, w, path_config=paths_with(test_mode=True)
        )

        assert found.review_cost.homework.status == "not_charged"
        assert found.review_cost.homework.sample.reviews == 0


class TestTheAddress:
    async def test_the_answer_names_no_one(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """No UUID anywhere in the body: no tenant, task, submission or person."""
        w = await bil.make_world(session_factory)
        await review(session_factory, w, [("0.10", FIRST)], e_usd="0.25")
        app.dependency_overrides[current_numbers] = serve_numbers_of(
            session_factory, w.tenant_id
        )
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/api/v1/public/numbers")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        assert response.json()["review_cost"]["homework"]["status"] == "ready"
        assert UUID.search(response.text) is None


@pytest.mark.requires_redis
class TestOneAnswerPerStart:
    async def test_the_processes_of_a_start_serve_the_same_numbers(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        arq_redis: ArqRedis,
    ) -> None:
        """Two processes of one start: the same bytes, as_of and ETag, though a
        review finished between them; another start computes anew."""
        w = await bil.make_world(session_factory)
        await review(session_factory, w, [("0.10", FIRST)], e_usd="0.25")

        async def compute() -> PublicNumbers:
            return await numbers(session_factory, w)

        key = f"public_numbers:test:{uuid.uuid4().hex}"
        first = await PublicNumbersStore(arq_redis, key, compute).get()
        await review(session_factory, w, [("0.50", FIRST)], e_usd="1.0")
        second = await PublicNumbersStore(arq_redis, key, compute).get()
        restarted = await PublicNumbersStore(arq_redis, f"{key}:2", compute).get()

        assert first is not None and second is not None and restarted is not None
        assert (second.body, second.etag) == (first.body, first.etag)
        assert second.numbers.as_of == first.numbers.as_of
        assert first.numbers.review_cost.homework.sample.reviews == 1
        assert restarted.numbers.review_cost.homework.sample.reviews == 2
        assert restarted.etag != first.etag
        assert 0 < (await arq_redis.ttl(key)) <= 7 * 24 * 3600
