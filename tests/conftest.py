"""Shared pytest fixtures."""

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

    for item in items:
        for marker_name, (should_skip, reason_msg) in skip_conditions.items():
            if should_skip and marker_name in item.keywords:
                item.add_marker(pytest.mark.skip(reason=reason_msg))
