"""Acceptance of task 07c, walked end to end: a draft checked before it is published.

The criterion this file exists for is a THROUGH one (acceptance 2 of task 07c,
``vision-rules#25``): a test is created by its title, kept unfinished, refused a
check and a publication while it is unfinished; finished, it is checked — one
generation, in the course's language, its explanations and a doubt read with the
draft — then published as it is, for no new job; edited, it has unpublished
changes and no check; checked again — one generation — and published again —
none. A student and a channel see nothing of it before its first publication.

So the walk goes only through the routes a client uses in production: the
author's routes with a key of scope PREP, the portal with a real bearer session
from a real login, the channel's route with a key of scope CHECK. The work the
routes queue is run by its real ARQ body, as the worker would, with a double of
the model that writes the register row a real call would have written.

The frame is task 07b's walk (``test_test_object_e2e_db.py``): its world, its
client, its ledger and its counters. Two things the register alone could not
show, measured the way that walk measures them:

- a call made while a request is served, outside any job, leaves NO register
  row — the register drops it and counts the drop (``DD-SP-AN``) — so every
  step asserts that no row was dropped;
- the queue's double keeps every call, queue named and all, and each check's
  job is read against the queue map of hot fix 5's lock
  (``tests/unit/test_queue_completeness.py``): a job goes to a queue whose
  worker runs it.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import copy
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from arq.constants import default_queue_name
from sqlalchemy import Text, cast, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import tests.integration.test_test_object_e2e_db as walk_07b
from course_supporter.homework.test_doors import MISSING_TASK
from course_supporter.jobs import JobType
from course_supporter.service_logging import get_current_job_id
from course_supporter.storage.orm import TaskReference
from tests.integration.test_test_object_e2e_db import (
    _SWITCHES,
    Client,
    _config,
    _dropped,
    _ExplanationsModel,
    _generations,
    _key,
    _ledger,
    _new_jobs,
    _nothing_asked,
    _one_generation,
    _only_in,
    _run_explanations,
    _structure,
    _student_session,
    _tenant_rows,
    _tree,
    _versions,
)
from tests.unit.test_queue_completeness import _served

pytestmark = pytest.mark.requires_db

# The walk of task 07b is the frame: its world and its client, by their names.
# Bound here rather than imported by name, so its own acceptance class is not
# collected a second time and no name is left unused.
world = walk_07b.world
client = walk_07b.client

_TITLE = "Тест до лекції 3 — у редакторі"
_UNFINISHED: dict[str, Any] = {
    "questions": [
        {
            "text": "Що виведе print(2 ** 3)?",
            "options": [{"text": "", "correct": False}],
        }
    ]
}
_UNFINISHED_PLACES = [
    {"code": "TEST_TEXT_EMPTY", "question": 1, "option": 1},
    {"code": "TEST_OPTIONS_COUNT", "question": 1, "option": None},
    {"code": "TEST_NO_CORRECT_OPTION", "question": 1, "option": None},
]
_FINISHED: dict[str, Any] = {
    "pass_threshold": 50,
    "questions": [
        {
            "text": "Що виведе print(2 ** 3)?",
            "options": [
                {"text": "6", "correct": False},
                {"text": "8", "correct": True},
            ],
        },
        {
            "text": "Скільки елементів у списку [1, 2, 3]?",
            "options": [
                {"text": "3", "correct": True},
                {"text": "2", "correct": False},
            ],
        },
    ],
}
_MODEL = {"1": "Два в кубі — вісім.", "2": "У списку три елементи: 1, 2 і 3."}
# The model doubts the author's answer to question 2. A version keeps only what
# is doubted, ``{number: true}`` (``agents/key_explainer.py``), so the double's
# ``false`` for question 1 is not stored.
_DOUBTED = {"2": True}
_OWN = _MODEL["1"] + " Оператор ** підносить до степеня."
_MODEL_2 = {"1": "Два в кубі — вісім, а не шість.", "2": "Елементів три, а не чотири."}
_NOT_CHECKED = {"state": "not_checked", "explanations": {}, "doubts": {}}
_IN_PROGRESS = {"state": "in_progress", "explanations": {}, "doubts": {}}
_READY = {"state": "ready", "explanations": _MODEL, "doubts": _DOUBTED}


class _DoubtingModel(_ExplanationsModel):
    """Task 07b's model double, with the doubts a check must show beside a question.

    The register row is written as 07b's double writes it; the answer then says
    which questions the model doubts, after task 07's walk
    (``test_test_submission_e2e_db.py``).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)
        self.doubts: dict[str, bool] = {}

    async def execute_for_stage(
        self,
        stage_name: str,
        *,
        response_validator: Any = None,
        expects_json: bool = False,
        **render_context: Any,
    ) -> Any:
        await super().execute_for_stage(
            stage_name, expects_json=expects_json, **render_context
        )
        if response_validator is not None:
            response_validator(
                json.dumps(
                    {
                        "explanations": self.explanations,
                        "doubts": {
                            number: self.doubts.get(number, False)
                            for number in self.explanations
                        },
                    }
                )
            )
        return None


def _put(queue: AsyncMock, since: int) -> list[tuple[str, str | None, str]]:
    """What was put on the queue since call ``since``: function, queue, job."""
    return [
        (call.args[0], call.kwargs.get("_queue_name"), call.args[1])
        for call in queue.enqueue_job.await_args_list[since:]
    ]


def _stranded(
    put: list[tuple[str, str | None, str]],
) -> list[tuple[str, str | None, str]]:
    """What went to a queue whose worker does not run it (hot fix 5's map)."""
    served = _served()
    return [
        (function, name, job)
        for function, name, job in put
        if function not in served.get(name or "", frozenset())
    ]


async def _the_models_rows(
    session_factory: async_sessionmaker[AsyncSession], test_id: uuid.UUID
) -> list[tuple[Any, ...]]:
    """The test's versions of explanations as stored — their JSON as its text."""
    async with session_factory() as session:
        result = await session.execute(
            select(
                TaskReference.id,
                TaskReference.state,
                cast(TaskReference.explanations, Text),
                cast(TaskReference.doubts, Text),
            )
            .where(TaskReference.authored_document_id == test_id)
            .order_by(TaskReference.version)
        )
        return [tuple(row) for row in result]


async def _unseen(
    client: Client,
    world: dict[str, uuid.UUID],
    test_id: uuid.UUID,
    bearer: dict[str, str],
    step: int,
) -> None:
    """Nothing of the test for a student or a channel: it answers as a missing one."""
    nodes, tests = await _tree(client, world["course_id"], bearer, step=step)
    assert str(world["section_id"]) in nodes, f"step {step}: the tree shows the section"
    assert tests == [], f"step {step}: no test in the student's tree"
    refused = [
        await client.ac.get(f"/api/v1/portal/tasks/{test_id}/test", headers=bearer)
    ]
    client.use_key(_key(world["tenant_id"], "check"))
    refused.append(await client.ac.get(f"/api/v1/homework/tasks/{test_id}/test"))
    client.use_key(_key(world["tenant_id"], "prep"))
    for response in refused:
        assert (response.status_code, response.json()["detail"]) == (
            404,
            MISSING_TASK,
        ), f"step {step}: {response.request.url.path}"


class TestAcceptance:
    async def test_a_checked_draft_is_published_without_paying_twice(
        self,
        client: Client,
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Acceptance 2 of task 07c, in one walk of ten steps.

        Each step is measured before and after — the jobs, the register rows
        and the rows dropped — so a step that began to pay, or stopped asking
        for what it must ask for, fails here, named by its number.
        """
        ac, queue = client.ac, client.queue
        tenant_id, course_id = world["tenant_id"], world["course_id"]
        model = _DoubtingModel(session_factory)
        bearer = await _student_session(client, world)
        client.use_key(_key(tenant_id, "prep"))

        # No job context leaks in from elsewhere: a register row written under
        # a stray one would count against the wrong job (hot fix 4).
        assert get_current_job_id() is None
        dropped = _dropped()

        with (
            patch(_SWITCHES[0], return_value=_config()),
            patch(_SWITCHES[1], return_value=_config()),
        ):
            # ── 1. A new test by its title alone: unfinished, nothing asked ──
            before = await _ledger(session_factory, tenant_id)
            created = await ac.post(
                f"/api/v1/nodes/{world['section_id']}/tests",
                json={"title": _TITLE, "questions": []},
            )
            assert created.status_code == 201, f"step 1: {created.text}"
            new = created.json()
            test_id = uuid.UUID(new["id"])
            assert new["incomplete"] == [
                {"code": "TEST_NO_QUESTIONS", "question": None, "option": None}
            ], "step 1: a test with no questions yet"
            assert (new["check"], new["published"], new["unpublished_changes"]) == (
                _NOT_CHECKED,
                None,
                True,
            ), "step 1: nothing checked, nothing published"
            assert new["course_root_id"] == str(course_id), "step 1: its course"
            await _nothing_asked(session_factory, tenant_id, before, dropped, 1)

            # ── 2. Unfinished, it is kept; its check and its publication are
            #       refused with every place, and nothing is asked for ────────
            before = await _ledger(session_factory, tenant_id)
            kept = await ac.put(f"/api/v1/tests/{test_id}/draft", json=_UNFINISHED)
            assert kept.status_code == 200, f"step 2: {kept.text}"
            assert kept.json()["incomplete"] == _UNFINISHED_PLACES, (
                "step 2: every place, in reading order"
            )
            for route in ("check", "publish"):
                refused = await ac.post(f"/api/v1/tests/{test_id}/{route}")
                assert refused.status_code == 422, f"step 2: {route}: {refused.text}"
                detail = refused.json()["detail"]
                assert (detail["code"], detail["incomplete"]) == (
                    "TEST_DRAFT_INCOMPLETE",
                    _UNFINISHED_PLACES,
                ), f"step 2: the {route} names every place"
            assert await _versions(session_factory, test_id) == [], "step 2: no version"
            await _nothing_asked(session_factory, tenant_id, before, dropped, 2)

            # ── 3. Finished: not checked, and nothing for a student or a channel
            before = await _ledger(session_factory, tenant_id)
            finished = await ac.put(f"/api/v1/tests/{test_id}/draft", json=_FINISHED)
            assert finished.status_code == 200, f"step 3: {finished.text}"
            draft = finished.json()
            assert (
                draft["incomplete"],
                draft["check"],
                draft["unpublished_changes"],
            ) == ([], _NOT_CHECKED, True), "step 3: finished, not checked"
            await _unseen(client, world, test_id, bearer, step=3)
            await _nothing_asked(session_factory, tenant_id, before, dropped, 3)

            # ── 4. The check: one job, in the course's language, on a queue whose
            #       worker runs it; its work writes the explanations and a doubt
            before = await _ledger(session_factory, tenant_id)
            since = len(queue.enqueue_job.await_args_list)
            checked = await ac.post(f"/api/v1/tests/{test_id}/check")
            assert checked.status_code == 200, f"step 4: {checked.text}"
            assert checked.json() == _IN_PROGRESS, "step 4: being written"
            asked = await _new_jobs(session_factory, tenant_id, before)
            assert [job.job_type for job in asked] == [JobType.KEY_EXPLANATION.value], (
                "step 4: exactly one job, the explanations'"
            )
            ((generation_1, language_1, form_1),) = await _generations(
                session_factory, test_id
            )
            assert (generation_1, language_1) == (asked[0].id, "ukr"), (
                "step 4: the job asked for, in the course's language"
            )
            put = _put(queue, since)
            assert _stranded(put) == [], "step 4: on a queue whose worker runs it"
            assert put == [
                ("arq_explain_key", default_queue_name, str(generation_1))
            ], "step 4: the check's job put on arq's default queue once"
            assert await _tenant_rows(session_factory, tenant_id) == before.rows, (
                "step 4: no register row in the request"
            )
            assert _dropped() == dropped, "step 4: no call outside a job"
            model.explanations, model.doubts = dict(_MODEL), {"2": True}
            rows_before = await _tenant_rows(session_factory, tenant_id)
            ran = await _run_explanations(session_factory, tenant_id, model)
            assert ran == [generation_1], "step 4: the job the check asked for ran"
            await _one_generation(session_factory, generation_1, step=4)
            await _only_in(session_factory, tenant_id, rows_before, ran, 4)
            assert _dropped() == dropped, "step 4: no call outside a job"
            before = await _ledger(session_factory, tenant_id)
            read = await ac.get(f"/api/v1/tests/{test_id}/draft")
            assert read.json()["check"] == _READY, (
                "step 4: the explanations and the doubt, by question"
            )
            await _unseen(client, world, test_id, bearer, step=4)
            await _nothing_asked(session_factory, tenant_id, before, dropped, 4)

            # ── 5. KD20: the author's own words, from the model's; the model's
            #       stay as written, and asking again costs nothing ──────────
            written = await _the_models_rows(session_factory, test_id)
            before = await _ledger(session_factory, tenant_id)
            edited = copy.deepcopy(_FINISHED)
            edited["questions"][0]["explanation"] = _OWN
            saved = await ac.put(f"/api/v1/tests/{test_id}/draft", json=edited)
            assert saved.status_code == 200, f"step 5: {saved.text}"
            assert saved.json()["draft"]["questions"][0]["explanation"] == _OWN, (
                "step 5: the author's own explanation"
            )
            assert saved.json()["check"] == _READY, "step 5: the same axes, the check"
            again = await ac.post(f"/api/v1/tests/{test_id}/check")
            assert again.status_code == 200, f"step 5: {again.text}"
            assert again.json() == _READY, "step 5: checked already — nothing asked"
            assert await _the_models_rows(session_factory, test_id) == written, (
                "step 5: the model's words as they were written"
            )
            await _nothing_asked(session_factory, tenant_id, before, dropped, 5)

            # ── 6. The publication of that very draft: version 1, no new job ──
            before = await _ledger(session_factory, tenant_id)
            published = await ac.post(f"/api/v1/tests/{test_id}/publish")
            assert published.status_code == 201, f"step 6: {published.text}"
            first = published.json()
            assert (first["created"], first["published"]["number"]) == (True, 1), (
                "step 6: the first version"
            )
            await _nothing_asked(session_factory, tenant_id, before, dropped, 6)
            version_1 = first["published"]["version"]
            assert version_1 == form_1, "step 6: the version the check explained"
            assert await _the_models_rows(session_factory, test_id) == written, (
                "step 6: the model's words as they were written"
            )
            read = await ac.get(f"/api/v1/tests/{test_id}/draft")
            assert (read.json()["unpublished_changes"], read.json()["check"]) == (
                False,
                _READY,
            ), "step 6: nothing unpublished, the check stands"
            _, tests = await _tree(client, course_id, bearer, step=6)
            assert tests == [(str(test_id), _TITLE)], "step 6: in the student's tree"
            sheet = await _structure(client, test_id, bearer, step=6)
            assert sheet["version"] == version_1, "step 6: version 1's form"
            flat = json.dumps(sheet, ensure_ascii=False)
            for secret in (_OWN, *_MODEL.values()):
                assert secret not in flat, f"step 6: {secret!r} crossed the route"
            client.use_key(_key(tenant_id, "check"))
            channel = await ac.get(f"/api/v1/homework/tasks/{test_id}/test")
            client.use_key(_key(tenant_id, "prep"))
            assert channel.json() == sheet, "step 6: the channel reads the same"
            await _nothing_asked(session_factory, tenant_id, before, dropped, 6)

            # ── 7. An option edited: unpublished changes, the check left behind
            before = await _ledger(session_factory, tenant_id)
            changed = copy.deepcopy(edited)
            changed["questions"][1]["options"][1]["text"] = "4"
            saved = await ac.put(f"/api/v1/tests/{test_id}/draft", json=changed)
            assert saved.status_code == 200, f"step 7: {saved.text}"
            assert (saved.json()["unpublished_changes"], saved.json()["check"]) == (
                True,
                _NOT_CHECKED,
            ), "step 7: a change not published, and not checked"
            await _nothing_asked(session_factory, tenant_id, before, dropped, 7)

            # ── 8. The second check: one job again, on its queue; its work written
            before = await _ledger(session_factory, tenant_id)
            since = len(queue.enqueue_job.await_args_list)
            checked = await ac.post(f"/api/v1/tests/{test_id}/check")
            assert checked.status_code == 200, f"step 8: {checked.text}"
            assert checked.json() == _IN_PROGRESS, "step 8: being written"
            asked = await _new_jobs(session_factory, tenant_id, before)
            assert [job.job_type for job in asked] == [JobType.KEY_EXPLANATION.value], (
                "step 8: exactly one job, the explanations'"
            )
            put = _put(queue, since)
            assert _stranded(put) == [], "step 8: on a queue whose worker runs it"
            assert put == [("arq_explain_key", default_queue_name, str(asked[0].id))], (
                "step 8: the check's job put on arq's default queue once"
            )
            assert await _tenant_rows(session_factory, tenant_id) == before.rows, (
                "step 8: no register row in the request"
            )
            assert _dropped() == dropped, "step 8: no call outside a job"
            model.explanations, model.doubts = dict(_MODEL_2), {}
            rows_before = await _tenant_rows(session_factory, tenant_id)
            ran = await _run_explanations(session_factory, tenant_id, model)
            assert ran == [asked[0].id], "step 8: the job the check asked for ran"
            await _one_generation(session_factory, asked[0].id, step=8)
            await _only_in(session_factory, tenant_id, rows_before, ran, 8)
            before = await _ledger(session_factory, tenant_id)
            read = await ac.get(f"/api/v1/tests/{test_id}/draft")
            assert read.json()["check"] == {
                "state": "ready",
                "explanations": _MODEL_2,
                "doubts": {},
            }, "step 8: the change explained"
            await _nothing_asked(session_factory, tenant_id, before, dropped, 8)

            # ── 9. The publication of the checked change: version 2, no new job
            before = await _ledger(session_factory, tenant_id)
            published = await ac.post(f"/api/v1/tests/{test_id}/publish")
            assert published.status_code == 201, f"step 9: {published.text}"
            second = published.json()
            assert (second["created"], second["published"]["number"]) == (True, 2), (
                "step 9: the second version"
            )
            await _nothing_asked(session_factory, tenant_id, before, dropped, 9)
            version_2 = second["published"]["version"]
            read = await ac.get(f"/api/v1/tests/{test_id}/draft")
            assert read.json()["unpublished_changes"] is False, "step 9: all published"
            sheet = await _structure(client, test_id, bearer, step=9)
            assert sheet["version"] == version_2, "step 9: version 2's form"
            await _nothing_asked(session_factory, tenant_id, before, dropped, 9)

            # ── 10. The whole walk: a generation per check, none per publication
            #        or per reading, every job on a queue whose worker runs it
            generations = await _generations(session_factory, test_id)
            assert [(language, form) for _, language, form in generations] == [
                ("ukr", version_1),
                ("ukr", version_2),
            ], "step 10: the walk's two generations, both a check's"
            put = _put(queue, 0)
            assert _stranded(put) == [], "step 10: every job where a worker runs it"
            assert put == [
                ("arq_explain_key", default_queue_name, str(job_id))
                for job_id, _, _ in generations
            ], "step 10: each check's job put on arq's default queue once"
            assert [v.version for v in await _versions(session_factory, test_id)] == [
                1,
                2,
            ], "step 10: two versions"
            assert _dropped() == dropped, "step 10: no call outside a job"
