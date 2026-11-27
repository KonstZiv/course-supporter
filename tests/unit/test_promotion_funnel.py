"""Б-4 without a database: the source-tag rule and the commands' arguments."""

from __future__ import annotations

from datetime import date

import pytest
from scripts import billing_admin
from structlog.testing import capture_logs

from course_supporter.billing.funnel import FUNNEL_SCOPES
from course_supporter.billing.promotion import PromotionPath
from course_supporter.open_courses.source_tag import parse_source_tag


class TestSourceTag:
    @pytest.mark.parametrize("tag", ["fb-dec-ads", "a", "x_1-2", "a" * 40, "2026"])
    def test_a_tag_that_keeps_the_rule_is_kept_silently(self, tag: str) -> None:
        with capture_logs() as logs:
            assert parse_source_tag(tag) == tag
        assert logs == []

    def test_none_is_none_silently(self) -> None:
        with capture_logs() as logs:
            assert parse_source_tag(None) is None
        assert logs == []

    @pytest.mark.parametrize(
        "raw",
        ["Bad Tag!", "a" * 41, "реклама", "", "FB", "ok\n", "a b", "a.b", 7, ["x"]],
        ids=repr,
    )
    def test_a_tag_that_breaks_it_is_dropped_with_a_line_without_the_value(
        self, raw: object
    ) -> None:
        with capture_logs() as logs:
            assert parse_source_tag(raw) is None
        assert logs == [{"event": "signup_source_tag_ignored", "log_level": "info"}]


class TestArguments:
    def test_funnel_needs_both_dates(self) -> None:
        parser = billing_admin.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["funnel", "--from", "2026-12-01"])

    def test_funnel_defaults(self) -> None:
        args = billing_admin.build_parser().parse_args(
            ["funnel", "--from", "2026-12-01", "--to", "2026-12-31"]
        )
        assert (args.from_date, args.to_date) == (date(2026, 12, 1), date(2026, 12, 31))
        assert (args.by, args.fmt, args.include_test_students) == (
            "path",
            "text",
            False,
        )

    @pytest.mark.parametrize("by", FUNNEL_SCOPES)
    def test_every_scope_is_accepted(self, by: str) -> None:
        args = billing_admin.build_parser().parse_args(
            ["funnel", "--from", "2026-12-01", "--to", "2026-12-31", "--by", by]
        )
        assert args.by == by

    def test_set_promotion_path_takes_the_paths_and_none_and_needs_an_actor(
        self,
    ) -> None:
        parser = billing_admin.build_parser()
        for path in [*(p.value for p in PromotionPath), "none"]:
            args = parser.parse_args(
                ["set-promotion-path", "--tenant", "t", "--path", path, "--actor", "k"]
            )
            assert args.path == path
        with pytest.raises(SystemExit):
            parser.parse_args(
                ["set-promotion-path", "--tenant", "t", "--path", "school"]
            )
        with pytest.raises(SystemExit):
            parser.parse_args(
                ["set-promotion-path", "--tenant", "t", "--path", "x", "--actor", "k"]
            )
