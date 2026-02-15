# PD-002: tenant_id на існуючих таблицях — Detail

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
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
    )
    tenant: Mapped["Tenant"] = relationship()
```

## Alembic Migration Strategy

Трьох-крокова міграція (в одному файлі):

```python
def upgrade() -> None:
    # 1. Add columns as NULLABLE
    op.add_column("courses", sa.Column("tenant_id", sa.Uuid(), nullable=True))
    op.add_column("llm_calls", sa.Column("tenant_id", sa.Uuid(), nullable=True))

    # 2. Create system tenant and backfill
    conn = op.get_bind()
    system_tenant_id = uuid7()
    conn.execute(
        sa.text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
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
    op.create_foreign_key(
        "fk_courses_tenant_id", "courses", "tenants",
        ["tenant_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "fk_llm_calls_tenant_id", "llm_calls", "tenants",
        ["tenant_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_courses_tenant_id", "courses", ["tenant_id"])
    op.create_index("ix_llm_calls_tenant_id", "llm_calls", ["tenant_id"])
```

## Тести

Файл: `tests/unit/test_tenant_isolation.py`

1. **test_course_requires_tenant_id** — Course без tenant_id → помилка
2. **test_course_with_tenant** — Course з tenant_id створюється
3. **test_llm_call_with_tenant** — LLMCall з tenant_id створюється
4. **test_cascade_tenant_delete_courses** — видалення tenant → видалення courses

Очікувана кількість тестів: **4**

## Definition of Done

- [ ] `tenant_id` FK на `courses` та `llm_calls`
- [ ] Alembic міграція з backfill (system tenant)
- [ ] NOT NULL constraint після backfill
- [ ] Індекси на нових FK
- [ ] 4 тести зелені
- [ ] `make check` зелений
- [ ] Документ оновлений відповідно до фінальної реалізації
