"""Orphan objects of the materials bucket — the parts that need no database (Б-2).

The key layouts the application writes and how they name a node; the grouping
of the report; what the dry-run output shows (counts and sizes, never a key);
the command line.
"""

from __future__ import annotations

import argparse
import uuid
from datetime import timedelta

import pytest
from scripts import storage_admin

from course_supporter.storage.orphans import (
    Category,
    Orphan,
    OrphanReport,
    category_of,
    origin_of,
)

TENANT = uuid.uuid4()
NODE = uuid.uuid4()
DOC = uuid.uuid4()
UPLOAD = uuid.uuid4()


class TestOriginOfAKey:
    @pytest.mark.parametrize(
        ("key", "tenant"),
        [
            (f"tenants/{TENANT}/nodes/{NODE}/{UPLOAD}/lecture.pdf", TENANT),
            (f"tenants/{TENANT}/nodes/{NODE}/slides/{DOC}/0001.webp", TENANT),
            (
                f"tenants/{TENANT}/nodes/{NODE}/bases/{DOC}/{UPLOAD}/original.zip",
                TENANT,
            ),
            (f"{NODE}/{UPLOAD}/lecture.pdf", None),
        ],
        ids=["presigned", "slide", "base", "upload_smart"],
    )
    def test_the_layouts_we_write_name_their_node(
        self, key: str, tenant: uuid.UUID | None
    ) -> None:
        origin = origin_of(key)
        assert origin is not None
        assert origin.node_id == NODE
        assert origin.tenant_id == tenant

    @pytest.mark.parametrize(
        "key",
        [
            "readme.txt",
            "backup/2026-11-01/dump.sql",
            f"tenants/{TENANT}/exports/{NODE}/report.csv",
            f"tenants/not-a-uuid/nodes/{NODE}/{UPLOAD}/a.pdf",
            f"tenants/{TENANT}/nodes/not-a-uuid/{UPLOAD}/a.pdf",
            f"tenants/{TENANT}/nodes/{NODE}",
            f"tenants/{TENANT}/nodes/{NODE}/",
            f"{NODE}/lecture.pdf",
            f"{NODE}/{UPLOAD}/deeper/lecture.pdf",
            f"{NODE}/not-a-uuid/lecture.pdf",
            f"{NODE}",
        ],
    )
    def test_any_other_layout_names_no_node(self, key: str) -> None:
        assert origin_of(key) is None

    def test_a_uuid_spelled_without_dashes_is_not_a_node(self) -> None:
        assert origin_of(f"{NODE.hex}/{UPLOAD}/a.pdf") is None


class TestCategoryOfAKey:
    base = f"tenants/{TENANT}/nodes/{NODE}/bases/{DOC}/{UPLOAD}"

    def test_a_base_archive(self) -> None:
        assert category_of(f"{self.base}/original.zip") is Category.ARCHIVE
        assert category_of(f"{self.base}/original.tar.gz") is Category.ARCHIVE

    def test_a_base_snapshot(self) -> None:
        assert category_of(f"{self.base}/snapshot.zip") is Category.SNAPSHOT

    def test_a_material_named_like_a_snapshot_is_other(self) -> None:
        assert category_of(f"{NODE}/{UPLOAD}/snapshot.zip") is Category.OTHER
        key = f"tenants/{TENANT}/nodes/{NODE}/{UPLOAD}/snapshot.zip"
        assert category_of(key) is Category.OTHER

    def test_slides_are_other(self) -> None:
        key = f"tenants/{TENANT}/nodes/{NODE}/slides/{DOC}/0001.webp"
        assert category_of(key) is Category.OTHER


class TestTheReport:
    def test_it_groups_by_category_and_counts_tenants(self) -> None:
        other_tenant = uuid.uuid4()
        report = OrphanReport()
        report.add(
            [
                Orphan("k1", 10, Category.ARCHIVE, TENANT),
                Orphan("k2", 20, Category.ARCHIVE, other_tenant),
                Orphan("k3", 5, Category.OTHER, TENANT),
                Orphan("k4", 7, Category.OTHER, None),
            ]
        )
        assert report.orphans == 4
        assert report.orphan_bytes == 42
        assert report.by_category[Category.ARCHIVE].objects == 2
        assert report.by_category[Category.ARCHIVE].bytes == 30
        assert report.by_category[Category.SNAPSHOT].objects == 0
        assert report.tenants == {TENANT, other_tenant}
        assert report.without_tenant == 1

    def test_an_orphan_does_not_show_its_key(self) -> None:
        orphan = Orphan("tenants/secret-key", 1, Category.OTHER, None)
        assert "secret-key" not in repr(orphan)


class TestTheOutput:
    def test_a_dry_run_shows_counts_and_sizes_and_no_key(self) -> None:
        report = OrphanReport(scanned=40, unresolved=3, too_young=2, live_node=20)
        report.referenced = 1
        key = f"tenants/{TENANT}/nodes/{NODE}/bases/{DOC}/{UPLOAD}/original.zip"
        report.add(
            [
                Orphan(key, 1_048_576, Category.ARCHIVE, TENANT),
                Orphan("other-key", 2_097_152, Category.OTHER, None),
            ]
        )
        out = storage_admin.render(report, dry_run=True, min_age=timedelta(hours=48))
        assert out == (
            "storage orphans  dry run: nothing written, nothing queued\n"
            "scanned 40 object(s), homework/ not listed; minimum age 48 h\n"
            "not orphans: 1 referenced by a live row, 20 of a live node, "
            "2 too young, 3 undetermined (the key names no node)\n"
            "orphans (no live reference; the node of the key deleted or missing):\n"
            "  archive        1 object(s)  1048576 B (1.0 MiB)\n"
            "  snapshot       0 object(s)  0 B (0.0 MiB)\n"
            "  other          1 object(s)  2097152 B (2.0 MiB)\n"
            "  total          2 object(s)  3145728 B (3.0 MiB)\n"
            "tenants: 1  (+ 1 object(s) with no tenant known)"
        )
        assert str(TENANT) not in out
        assert "other-key" not in out

    def test_a_real_run_adds_the_jobs_and_says_when_one_was_not_taken(self) -> None:
        report = OrphanReport(jobs=3, jobs_not_dispatched=1)
        out = storage_admin.render(report, dry_run=False, min_age=timedelta(hours=1))
        assert out.startswith("storage orphans  queued\n")
        assert out.endswith(
            "cleanup jobs created: 3  NOT DISPATCHED: 1 (run the command again)"
        )


class TestTheCommandLine:
    def test_it_wants_an_actor(self) -> None:
        with pytest.raises(SystemExit) as exc:
            storage_admin.build_parser().parse_args(["orphans"])
        assert exc.value.code == 2

    def test_the_defaults(self) -> None:
        args = storage_admin.build_parser().parse_args(["orphans", "--actor", "kos"])
        assert args.min_age_hours == 48
        assert args.dry_run is False

    def test_it_takes_the_options(self) -> None:
        args = storage_admin.build_parser().parse_args(
            ["orphans", "--actor", "kos", "--min-age-hours", "0.5", "--dry-run"]
        )
        assert args.min_age_hours == 0.5
        assert args.dry_run is True

    @pytest.mark.parametrize("value", ["-1", "soon", "nan", "inf"])
    def test_a_bad_age_is_a_usage_error(self, value: str) -> None:
        with pytest.raises((SystemExit, argparse.ArgumentTypeError)):
            storage_admin.build_parser().parse_args(
                ["orphans", "--actor", "kos", "--min-age-hours", value]
            )
