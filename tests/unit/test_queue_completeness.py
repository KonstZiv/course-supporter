"""Completeness lock: every job the code puts on an ARQ queue is one that queue runs.

Hot fix 5 (mentor-rebuild, task 07). The explanations a review asked for after
delivery were enqueued with no queue named, so they took the default queue of
the pool they were handed — the homework worker's own, ``homework`` — where
nothing registers ``arq_explain_key``. arq fails such a job as "function not
found" before any code of ours runs; the ``jobs`` row stays ``queued`` and holds
its subject, and the work is never done.

Where a call that names no queue lands depends on who built the pool, which no
reading of the call can tell. So the lock is read off the source, over every
place that enqueues — each ``enqueue_job`` call and each reactivation dispatch:
it names its queue, and the worker settings serving that queue register the
function. A sibling of ``test_seam_completeness.py``, over the same two worker
settings.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from arq.constants import default_queue_name
from arq.worker import func as arq_function

import course_supporter
from course_supporter.worker import HomeworkWorkerSettings, WorkerSettings

_PACKAGE = Path(course_supporter.__file__).parent

# The one name a call may give a queue besides a literal: arq's own default.
_QUEUE_NAMES = {"default_queue_name": default_queue_name}

# What enqueued when this lock was written — the positive control, so a scan
# that silently found nothing could not pass the checks below.
_KNOWN_ENQUEUED = frozenset(
    {
        "arq_ingest_material",
        "arq_process_homework",
        "arq_regenerate_node_summary",
        "base_normalize_task",
        "arq_explain_key",
        "arq_prepare_document",
        "arq_send_email",
        "s3_cleanup_task",
    }
)
_KNOWN_REACTIVATED = frozenset(
    {
        "arq_ingest_material",
        "arq_process_homework",
        "s3_cleanup_task",
        "arq_regenerate_node_summary",
    }
)


@dataclass(frozen=True)
class _Enqueue:
    """One place that puts a function on a queue, as the source says it."""

    where: str
    module: str
    # None when the call reads it at run time — a reactivation's dispatch.
    function: str | None
    # None when the place names no queue.
    queue: ast.expr | None


def _literal(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _enqueues() -> tuple[list[_Enqueue], list[_Enqueue]]:
    """Every ``enqueue_job`` call and every reactivation dispatch in the package."""
    calls: list[_Enqueue] = []
    dispatches: list[_Enqueue] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        module = str(path.relative_to(_PACKAGE.parent))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=module)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            where = f"{module}:{node.lineno}"
            if isinstance(node.func, ast.Attribute) and node.func.attr == "enqueue_job":
                first = node.args[0] if node.args else None
                calls.append(
                    _Enqueue(
                        where, module, _literal(first), _keyword(node, "_queue_name")
                    )
                )
            elif (
                isinstance(node.func, ast.Name) and node.func.id == "_ReenqueueDispatch"
            ):
                dispatches.append(
                    _Enqueue(
                        where,
                        module,
                        _literal(_keyword(node, "arq_function")),
                        _keyword(node, "queue_name"),
                    )
                )
    return calls, dispatches


def _queue(node: ast.expr | None) -> str | None:
    """The queue a place names — a literal, or arq's default by its name."""
    if isinstance(node, ast.Name):
        return _QUEUE_NAMES.get(node.id)
    return _literal(node)


def _served() -> dict[str, frozenset[str]]:
    """Each queue a worker serves → what it runs, named the way arq names it."""
    return {
        getattr(settings, "queue_name", default_queue_name): frozenset(
            arq_function(fn).name for fn in settings.functions
        )
        for settings in (WorkerSettings, HomeworkWorkerSettings)
    }


def test_the_lock_finds_every_known_enqueue() -> None:
    calls, dispatches = _enqueues()

    assert _KNOWN_ENQUEUED.issubset(call.function for call in calls)
    assert _KNOWN_REACTIVATED.issubset(entry.function for entry in dispatches)
    assert set(_served()) == {default_queue_name, "homework"}


def test_every_enqueue_names_its_queue() -> None:
    calls, dispatches = _enqueues()

    unnamed = [
        place.where
        for place in (*calls, *dispatches)
        if place.function is not None and _queue(place.queue) is None
    ]

    assert not unnamed, f"enqueued with no queue named: {unnamed}"


def test_a_function_read_at_run_time_comes_from_a_checked_dispatch() -> None:
    """A call whose function the source does not name is allowed only beside the
    reactivation dispatch it reads — whose entries the two checks here cover."""
    calls, dispatches = _enqueues()
    with_a_table = {entry.module for entry in dispatches}

    unread = [
        call.where
        for call in calls
        if call.function is None and call.module not in with_a_table
    ]

    assert with_a_table, "the reactivation dispatch was found"
    assert not unread, f"enqueued a function no reading can name: {unread}"


def test_every_queue_runs_what_is_put_on_it() -> None:
    served = _served()
    calls, dispatches = _enqueues()

    stranded = [
        f"{place.where}: {place.function} on {queue!r}"
        for place in (*calls, *dispatches)
        if place.function is not None
        and (queue := _queue(place.queue)) is not None
        and place.function not in served.get(queue, frozenset())
    ]

    assert not stranded, f"put on a queue whose worker does not run it: {stranded}"
