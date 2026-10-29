"""Shared pytest fixtures."""

import shutil
from collections.abc import Iterator

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-db",
        action="store_true",
        default=False,
        help="Run tests that require a live PostgreSQL instance",
    )
    parser.addoption(
        "--run-redis",
        action="store_true",
        default=False,
        help="Run tests that require a live Redis instance",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    markers_to_check = {
        "requires_db": ("--run-db", "needs --run-db flag"),
        "requires_redis": ("--run-redis", "needs --run-redis flag"),
    }

    skip_conditions = {
        marker_name: (not config.getoption(option_flag), reason_msg)
        for marker_name, (option_flag, reason_msg) in markers_to_check.items()
    }

    # ffmpeg is orthogonal to the DB/Redis flags: gate on binary
    # presence (auto-skip if absent), not a CLI flag, so the skip reason
    # names its own dependency cleanly.
    ffmpeg_missing = shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None

    for item in items:
        for marker_name, (should_skip, reason_msg) in skip_conditions.items():
            if should_skip and marker_name in item.keywords:
                item.add_marker(pytest.mark.skip(reason=reason_msg))
        if ffmpeg_missing and "requires_ffmpeg" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="needs ffmpeg/ffprobe on PATH"))


@pytest.fixture(autouse=True)
def _every_paid_call_leaves_its_row(request: pytest.FixtureRequest) -> Iterator[None]:
    """Lock (BIL-C, decision 6): a paid call always runs inside a job context.

    The call register is the money source, and a paid call whose row is dropped
    — no job in context, or a write the database refused — is money nobody will
    ever see. ``_persist`` counts such drops (``dropped_paid_call_count``);
    any test during which one happens fails here. A test that drops one on
    purpose says so with ``@pytest.mark.drops_paid_calls``.

    Red on the mutation that removes ``set_job_from_arq`` from the execution
    seam: the worker entries that rely on it (``arq_explain_key``) then make
    their paid calls with no job in context.
    """
    from course_supporter.service_logging import dropped_paid_call_count

    before = dropped_paid_call_count()
    yield
    if request.node.get_closest_marker("drops_paid_calls") is not None:
        return
    dropped = dropped_paid_call_count() - before
    assert dropped == 0, (
        f"{dropped} paid call(s) left no register row during this test: made "
        "outside a job context or refused by the database (see the "
        "esc_write_skipped_no_job_id / service_call_log_failed ERROR lines)"
    )
