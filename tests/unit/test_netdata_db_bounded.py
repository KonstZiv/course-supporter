"""Test that netdata keeps its metrics database bounded and on its own volume."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

DB_LIMITS = (
    "dbengine tier 0 retention size = 256MiB",
    "dbengine tier 0 retention time = 7d",
    "dbengine tier 1 retention size = 128MiB",
    "dbengine tier 1 retention time = 1mo",
    "dbengine tier 2 retention size = 64MiB",
    "dbengine tier 2 retention time = 6mo",
)


def test_netdata_conf_bounds_the_metrics_database() -> None:
    conf = REPO_ROOT / "deploy" / "netdata" / "netdata.conf"
    lines = [line.strip() for line in conf.read_text(encoding="utf-8").splitlines()]
    assert lines.count("[db]") == 1, "Expected one [db] section in netdata.conf"
    start = lines.index("[db]") + 1
    end = next(
        (i for i in range(start, len(lines)) if lines[i].startswith("[")),
        len(lines),
    )
    for limit in DB_LIMITS:
        assert limit in lines[start:end], f"Missing in [db] of netdata.conf: {limit}"


def test_compose_mounts_netdata_conf_and_cache_volume() -> None:
    compose_file = REPO_ROOT / "docker-compose.prod.yaml"
    compose = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    mounts = compose["services"]["netdata"]["volumes"]
    assert "./deploy/netdata/netdata.conf:/etc/netdata/netdata.conf:ro" in mounts
    assert "netdata-cache:/var/cache/netdata" in mounts
    assert "netdata-cache" in compose["volumes"]
