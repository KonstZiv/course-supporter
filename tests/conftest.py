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
    run_db = config.getoption("--run-db")
    run_redis = config.getoption("--run-redis")

    if not run_db:
        skip_db = pytest.mark.skip(reason="needs --run-db flag")
        for item in items:
            if "requires_db" in item.keywords:
                item.add_marker(skip_db)

    if not run_redis:
        skip_redis = pytest.mark.skip(reason="needs --run-redis flag")
        for item in items:
            if "requires_redis" in item.keywords:
                item.add_marker(skip_redis)
