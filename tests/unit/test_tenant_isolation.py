"""Tests for tenant_id on existing tables (courses, external_service_calls)."""

from __future__ import annotations

from course_supporter.storage.orm import Course, ExternalServiceCall, _uuid7


class TestCourseTenant:
    """Tests for tenant_id on Course model."""

    def test_course_has_tenant_id_column(self) -> None:
        """Course table has tenant_id FK column."""
        table = Course.__table__
        col = table.c.tenant_id
        assert col is not None
        assert col.nullable is False

    def test_course_with_tenant(self) -> None:
        """Course accepts tenant_id at construction."""
        tid = _uuid7()
        course = Course(tenant_id=tid, title="Python 101")
        assert course.tenant_id == tid
        assert course.title == "Python 101"

    def test_course_tenant_fk_cascade(self) -> None:
        """Course.tenant_id FK has CASCADE ondelete."""
        table = Course.__table__
        fks = [fk for fk in table.foreign_keys if fk.column.table.name == "tenants"]
        assert len(fks) == 1
        assert fks[0].ondelete == "CASCADE"

    def test_course_tenant_id_indexed(self) -> None:
        """Course.tenant_id has an index."""
        table = Course.__table__
        col = table.c.tenant_id
        assert col.index is True


class TestExternalServiceCallTenant:
    """Tests for tenant_id on ExternalServiceCall model."""

    def test_has_tenant_id_column(self) -> None:
        """ExternalServiceCall table has nullable tenant_id FK column."""
        table = ExternalServiceCall.__table__
        col = table.c.tenant_id
        assert col is not None
        assert col.nullable is True

    def test_with_tenant(self) -> None:
        """ExternalServiceCall accepts tenant_id at construction."""
        tid = _uuid7()
        call = ExternalServiceCall(
            tenant_id=tid, provider="gemini", model_id="gemini-2.0-flash"
        )
        assert call.tenant_id == tid

    def test_tenant_fk_cascade(self) -> None:
        """ExternalServiceCall.tenant_id FK has CASCADE ondelete."""
        table = ExternalServiceCall.__table__
        fks = [fk for fk in table.foreign_keys if fk.column.table.name == "tenants"]
        assert len(fks) == 1
        assert fks[0].ondelete == "CASCADE"

    def test_tenant_id_indexed(self) -> None:
        """ExternalServiceCall.tenant_id has an index."""
        table = ExternalServiceCall.__table__
        col = table.c.tenant_id
        assert col.index is True

    def test_has_job_id_column(self) -> None:
        """ExternalServiceCall has nullable job_id FK."""
        table = ExternalServiceCall.__table__
        col = table.c.job_id
        assert col is not None
        assert col.nullable is True

    def test_has_unit_type_column(self) -> None:
        """ExternalServiceCall has unit_type column."""
        table = ExternalServiceCall.__table__
        col = table.c.unit_type
        assert col is not None
        assert col.nullable is True

    def test_has_prompt_ref_column(self) -> None:
        """ExternalServiceCall has prompt_ref (renamed from prompt_version)."""
        table = ExternalServiceCall.__table__
        columns = {c.name for c in table.columns}
        assert "prompt_ref" in columns
        assert "prompt_version" not in columns

    def test_has_unit_in_out_columns(self) -> None:
        """ExternalServiceCall has unit_in/unit_out (renamed from tokens_*)."""
        table = ExternalServiceCall.__table__
        columns = {c.name for c in table.columns}
        assert "unit_in" in columns
        assert "unit_out" in columns
        assert "tokens_in" not in columns
        assert "tokens_out" not in columns
