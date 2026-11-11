"""Unit tests for the ``billing_admin.py costs`` report and renderer."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from scripts import billing_admin

from course_supporter.billing import costs as billing_costs
from course_supporter.billing.errors import BillingNotFoundError


def _sample_report(
    *,
    by: billing_costs.CostReportScope = "both",
    with_unknown: bool = True,
) -> billing_costs.CostReport:
    t1_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    t2_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    s1_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    s2_id = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

    unknown_t1 = 2 if with_unknown else 0

    a1 = billing_costs.AuthorCostRow(
        tenant_id=t1_id,
        tenant_name="Alpha Academy",
        spend=billing_costs.SpendSummary(
            calls=5 + unknown_t1,
            exact_calls=3,
            exact_usd=Decimal("0.000450"),
            estimated_calls=2,
            estimated_usd=Decimal("0.100000"),
            unknown_calls=unknown_t1,
        ),
        processing=billing_costs.ActionSpend(
            calls=2, known_usd=Decimal("0.040000"), unknown_calls=0
        ),
        task_preparation=billing_costs.ActionSpend(
            calls=1 + (1 if with_unknown else 0),
            known_usd=Decimal("0.000450"),
            unknown_calls=1 if with_unknown else 0,
        ),
        review=billing_costs.ActionSpend(
            calls=2 + (1 if with_unknown else 0),
            known_usd=Decimal("0.060000"),
            unknown_calls=1 if with_unknown else 0,
        ),
        other=billing_costs.ActionSpend.zero(),
    )
    a2 = billing_costs.AuthorCostRow(
        tenant_id=t2_id,
        tenant_name="Beta School",
        spend=billing_costs.SpendSummary(
            calls=1,
            exact_calls=1,
            exact_usd=Decimal("0.0000005"),
            estimated_calls=0,
            estimated_usd=Decimal("0"),
            unknown_calls=0,
        ),
        processing=billing_costs.ActionSpend.zero(),
        task_preparation=billing_costs.ActionSpend.zero(),
        review=billing_costs.ActionSpend(
            calls=1, known_usd=Decimal("0.0000005"), unknown_calls=0
        ),
        other=billing_costs.ActionSpend.zero(),
    )

    authors = (a1, a2) if by in ("author", "both") else ()
    authors_total = (
        billing_costs.AuthorCostTotals(
            spend=a1.spend + a2.spend,
            processing=a1.processing + a2.processing,
            task_preparation=a1.task_preparation + a2.task_preparation,
            review=a1.review + a2.review,
            other=billing_costs.ActionSpend.zero(),
        )
        if by in ("author", "both")
        else billing_costs.AuthorCostTotals(
            spend=billing_costs.SpendSummary.zero(),
            processing=billing_costs.ActionSpend.zero(),
            task_preparation=billing_costs.ActionSpend.zero(),
            review=billing_costs.ActionSpend.zero(),
            other=billing_costs.ActionSpend.zero(),
        )
    )

    st1_unknown = 1 if with_unknown else 0
    st1 = billing_costs.StudentCostRow(
        tenant_id=t1_id,
        tenant_name="Alpha Academy",
        student_id=s1_id,
        submissions=2,
        spend=billing_costs.SpendSummary(
            calls=2 + st1_unknown,
            exact_calls=1,
            exact_usd=Decimal("0.020000"),
            estimated_calls=1,
            estimated_usd=Decimal("0.040000"),
            unknown_calls=st1_unknown,
        ),
    )
    st2 = billing_costs.StudentCostRow(
        tenant_id=t2_id,
        tenant_name="Beta School",
        student_id=s2_id,
        submissions=1,
        spend=billing_costs.SpendSummary(
            calls=1,
            exact_calls=1,
            exact_usd=Decimal("0.0000005"),
            estimated_calls=0,
            estimated_usd=Decimal("0"),
            unknown_calls=0,
        ),
    )

    students = (st1, st2) if by in ("student", "both") else ()
    students_total = (
        billing_costs.StudentCostTotals(submissions=3, spend=st1.spend + st2.spend)
        if by in ("student", "both")
        else billing_costs.StudentCostTotals(
            submissions=0, spend=billing_costs.SpendSummary.zero()
        )
    )

    return billing_costs.CostReport(
        from_date=date(2026, 11, 1),
        to_date=date(2026, 11, 11),
        tenant_id=None,
        by=by,
        authors=authors,
        authors_total=authors_total,
        students=students,
        students_total=students_total,
    )


class TestDefaultUtcMonthBounds:
    """Default month window strictly follows the UTC calendar."""

    def test_mid_month_utc(self) -> None:
        now = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
        assert billing_costs.default_month_bounds(now) == (
            date(2026, 11, 1),
            date(2026, 11, 11),
        )

    def test_positive_offset_crossing_month_boundary_uses_utc(self) -> None:
        # 2026-11-01 00:15 in Kyiv (+02:00) is 2026-10-31 22:15 UTC.
        kyiv = timezone(timedelta(hours=2))
        now = datetime(2026, 11, 1, 0, 15, tzinfo=kyiv)
        assert billing_costs.default_month_bounds(now) == (
            date(2026, 10, 1),
            date(2026, 10, 31),
        )


class TestCostsParserAndDispatch:
    """CLI parsing, date validation, and delegation for ``costs``."""

    def test_parser_defaults(self) -> None:
        args = billing_admin.build_parser().parse_args(["costs"])
        assert args.command == "costs"
        assert args.from_date is None
        assert args.to_date is None
        assert args.tenant is None
        assert args.by == "both"
        assert args.fmt == "table"
        assert not hasattr(args, "actor")

    @pytest.mark.parametrize("flag", ["--from", "--to"])
    @pytest.mark.parametrize(
        "bad_value",
        ["not-a-date", "2026-02-30", "20261101", "2026-11-01T00:00:00"],
    )
    def test_refuses_bad_date(self, flag: str, bad_value: str) -> None:
        with pytest.raises(SystemExit) as exc:
            billing_admin.build_parser().parse_args(["costs", flag, bad_value])
        assert exc.value.code == 2

    def test_refuses_from_after_to(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        async def run_stub(args: object) -> str:
            session = AsyncMock()
            session.get_bind = lambda: None
            return await billing_admin.costs_cmd(session, args)  # type: ignore[arg-type]

        monkeypatch.setattr(billing_admin, "run", run_stub)

        rc = billing_admin.main(["costs", "--from", "2026-11-15", "--to", "2026-11-01"])
        assert rc == 1
        assert "refused:" in capsys.readouterr().err

    def test_defaults_window_to_current_utc_month(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        async def fake_build(
            session: object,
            *,
            from_date: date,
            to_date: date,
            tenant_id: uuid.UUID | None,
            by: billing_costs.CostReportScope,
        ) -> billing_costs.CostReport:
            captured["from_date"] = from_date
            captured["to_date"] = to_date
            captured["tenant_id"] = tenant_id
            captured["by"] = by
            return _sample_report(by=by)

        async def fake_run(args: object) -> str:
            session = AsyncMock()
            session.get_bind = lambda: None
            return await billing_admin.costs_cmd(session, args)  # type: ignore[arg-type]

        monkeypatch.setattr(
            billing_costs,
            "default_month_bounds",
            lambda: (date(2026, 11, 1), date(2026, 11, 11)),
        )
        monkeypatch.setattr(billing_costs, "build_cost_report", fake_build)
        monkeypatch.setattr(billing_admin, "run", fake_run)

        rc = billing_admin.main(["costs", "--by", "student"])
        assert rc == 0
        assert captured == {
            "from_date": date(2026, 11, 1),
            "to_date": date(2026, 11, 11),
            "tenant_id": None,
            "by": "student",
        }

    def test_refuses_unknown_tenant_uuid(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = uuid.UUID("99999999-9999-9999-9999-999999999999")

        async def fake_run(args: object) -> str:
            session = AsyncMock()
            session.get_bind = lambda: None
            session.get = AsyncMock(return_value=None)
            return await billing_admin.costs_cmd(session, args)  # type: ignore[arg-type]

        monkeypatch.setattr(billing_admin, "run", fake_run)

        rc = billing_admin.main(["costs", "--tenant", str(missing)])
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_missing_named_tenant_raises_not_found(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        async def fake_resolve(session: object, value: str) -> uuid.UUID:
            raise BillingNotFoundError(f"tenant {value!r} not found")

        async def fake_run(args: object) -> str:
            session = AsyncMock()
            session.get_bind = lambda: None
            return await billing_admin.costs_cmd(session, args)  # type: ignore[arg-type]

        monkeypatch.setattr(billing_admin, "resolve_tenant_id", fake_resolve)
        monkeypatch.setattr(billing_admin, "run", fake_run)

        rc = billing_admin.main(["costs", "--tenant", "No Such Academy"])
        assert rc == 1
        assert "not found" in capsys.readouterr().err


class TestCostReportRendering:
    """Table and CSV renderers cover totals, unknown line, precision, and no PII."""

    def test_table_both_shows_totals_unknown_lines_and_small_usd(self) -> None:
        report = _sample_report(by="both", with_unknown=True)
        out = billing_admin._render_cost_table(report, tenant_label=None)

        assert "Період (UTC): 2026-11-01 .. 2026-11-11 (включно) | Орендар: усі" in out
        assert "По авторах:" in out
        assert "По студентах:" in out
        assert out.count("разом") == 2
        assert "вартість невідома: 2 викликів" in out
        assert "вартість невідома: 1 викликів" in out
        # Sub-microdollar costs must not collapse to $0.00.
        assert "$0.0000005" in out
        assert "$0.1004505" in out

    def test_table_author_only_and_zero_unknown(self) -> None:
        report = _sample_report(by="author", with_unknown=False)
        out = billing_admin._render_cost_table(report, tenant_label="Alpha Academy")

        assert "По авторах:" in out
        assert "По студентах:" not in out
        assert "Орендар: Alpha Academy" in out
        assert "вартість невідома: 0 викликів" in out

    def test_csv_both_parses_cleanly_with_totals_and_unknown_notes(self) -> None:
        report = _sample_report(by="both", with_unknown=True)
        raw = billing_admin._render_cost_csv(report)
        rows = list(csv.DictReader(io.StringIO(raw)))

        author_rows = [
            r for r in rows if r["section"] == "author" and r["row_kind"] == "row"
        ]
        author_tot = [
            r for r in rows if r["section"] == "author" and r["row_kind"] == "total"
        ]
        student_rows = [
            r for r in rows if r["section"] == "student" and r["row_kind"] == "row"
        ]
        student_tot = [
            r for r in rows if r["section"] == "student" and r["row_kind"] == "total"
        ]

        assert len(author_rows) == 2
        assert len(author_tot) == 1
        assert author_tot[0]["tenant_name"] == "разом"
        assert author_tot[0]["calls"] == "8"
        assert author_tot[0]["unknown_calls"] == "2"
        assert author_tot[0]["note"] == "вартість невідома: 2 викликів"
        assert Decimal(author_tot[0]["total_usd"]) == Decimal("0.1004505")

        assert len(student_rows) == 2
        assert len(student_tot) == 1
        assert student_tot[0]["tenant_name"] == "разом"
        assert student_tot[0]["submissions"] == "3"
        assert student_tot[0]["calls"] == "4"
        assert student_tot[0]["unknown_calls"] == "1"
        assert student_tot[0]["note"] == "вартість невідома: 1 викликів"
