"""Tests for tenant_id on existing tables (courses, llm_calls)."""

from __future__ import annotations

from course_supporter.storage.orm import Course, LLMCall, _uuid7


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


class TestLLMCallTenant:
    """Tests for tenant_id on LLMCall model."""

    def test_llm_call_has_tenant_id_column(self) -> None:
        """LLMCall table has nullable tenant_id FK column."""
        table = LLMCall.__table__
        col = table.c.tenant_id
        assert col is not None
        assert col.nullable is True

    def test_llm_call_with_tenant(self) -> None:
        """LLMCall accepts tenant_id at construction."""
        tid = _uuid7()
        call = LLMCall(tenant_id=tid, provider="gemini", model_id="gemini-2.0-flash")
        assert call.tenant_id == tid

    def test_llm_call_tenant_fk_cascade(self) -> None:
        """LLMCall.tenant_id FK has CASCADE ondelete."""
        table = LLMCall.__table__
        fks = [fk for fk in table.foreign_keys if fk.column.table.name == "tenants"]
        assert len(fks) == 1
        assert fks[0].ondelete == "CASCADE"

    def test_llm_call_tenant_id_indexed(self) -> None:
        """LLMCall.tenant_id has an index."""
        table = LLMCall.__table__
        col = table.c.tenant_id
        assert col.index is True
