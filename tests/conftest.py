"""Shared pytest fixtures."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-db",
        action="store_true",
        default=False,
        help="Run tests that require a live PostgreSQL instance",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--run-db"):
        return
    skip_db = pytest.mark.skip(reason="needs --run-db flag")
    for item in items:
        if "requires_db" in item.keywords:
            item.add_marker(skip_db)
