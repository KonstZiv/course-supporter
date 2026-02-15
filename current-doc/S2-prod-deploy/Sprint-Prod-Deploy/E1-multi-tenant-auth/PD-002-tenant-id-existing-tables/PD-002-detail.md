# PD-002: tenant_id на існуючих таблицях — Detail ✅

## Контекст

Після PD-001 існують моделі Tenant та APIKey. Тепер потрібно прив'язати існуючі бізнес-таблиці до tenant для ізоляції даних та білінгу.

## Зміни в ORM

### Course

```python
class Course(Base):
    __tablename__ = "courses"
    # ... existing fields ...
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
    )
    # ... existing relationships ...
    tenant: Mapped["Tenant"] = relationship()
```

### LLMCall

```python
class LLMCall(Base):
    __tablename__ = "llm_calls"
    # ... existing fields ...
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
    )
    tenant: Mapped["Tenant | None"] = relationship()
```

> **Примітка:** `LLMCall.tenant_id` зроблено nullable в PD-006 (міграція `d3d44a540129`), оскільки global ModelRouter не має tenant context при створенні (background tasks, evals).

## Alembic Migrations

### 1. `ec62b2f8d538_add_tenant_id_to_courses_and_llm_calls.py`

Трьох-крокова міграція:

```python
def upgrade() -> None:
    # 1. Add columns as NULLABLE
    op.add_column("courses", sa.Column("tenant_id", sa.Uuid(), nullable=True))
    op.add_column("llm_calls", sa.Column("tenant_id", sa.Uuid(), nullable=True))

    # 2. Create system tenant and backfill
    conn = op.get_bind()
    system_tenant_id = str(uuid7_lib.uuid7())
    conn.execute(
        sa.text("INSERT INTO tenants (id, name, is_active) VALUES (:id, :name, true)"),
        {"id": system_tenant_id, "name": "system"},
    )
    conn.execute(
        sa.text("UPDATE courses SET tenant_id = :tid WHERE tenant_id IS NULL"),
        {"tid": system_tenant_id},
    )
    conn.execute(
        sa.text("UPDATE llm_calls SET tenant_id = :tid WHERE tenant_id IS NULL"),
        {"tid": system_tenant_id},
    )

    # 3. Set NOT NULL + FK + index
    op.alter_column("courses", "tenant_id", nullable=False)
    op.alter_column("llm_calls", "tenant_id", nullable=False)
    op.create_foreign_key(...)
    op.create_index(...)
```

### 2. `d3d44a540129_make_llm_calls_tenant_id_nullable.py` (PD-006)

```python
def upgrade() -> None:
    op.alter_column('llm_calls', 'tenant_id', existing_type=sa.UUID(), nullable=True)
```

## Тести

Файл: `tests/unit/test_tenant_isolation.py` — **8 тестів**

1. `test_course_has_tenant_id_column` — Course.tenant_id FK exists, NOT NULL
2. `test_course_with_tenant` — Course приймає tenant_id
3. `test_llm_call_has_tenant_id_column` — LLMCall.tenant_id FK exists, **nullable**
4. `test_llm_call_with_tenant` — LLMCall приймає tenant_id
5. Plus 4 additional ORM relationship and constraint tests

## Definition of Done

- [x] `tenant_id` FK на `courses` (NOT NULL) та `llm_calls` (nullable)
- [x] Alembic міграція з backfill (system tenant)
- [x] Індекси на нових FK
- [x] 8 тестів зелені
- [x] `make check` зелений
