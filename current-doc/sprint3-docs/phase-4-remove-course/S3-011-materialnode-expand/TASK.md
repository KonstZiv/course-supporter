# S3-011: Expand MaterialNode (Additive Changes)

**Phase:** 4a (Remove Course — Preparation)
**Складність:** M
**Статус:** PENDING
**Залежність:** Phases 0-3

## Контекст

Підготовка MaterialNode для ролі "курсу" — додати поля що зараз є на Course, та `tenant_id` для direct tenant isolation.

## Нові поля на MaterialNode

| Field | Type | Source |
|-------|------|--------|
| `tenant_id` | UUID FK → Tenant | Data migration: copy з Course |
| `learning_goal` | Text, nullable | Нове (було тільки на Course) |
| `expected_knowledge` | JSONB, nullable | Нове (було тільки на Course) |
| `expected_skills` | JSONB, nullable | Нове (було тільки на Course) |

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `src/course_supporter/storage/orm.py` | ADD 4 columns на MaterialNode |
| `src/course_supporter/api/schemas.py` | Update MaterialNodeResponse |
| `migrations/versions/` | ADD COLUMN x4 + data migration |
| `tests/` | Update factory/fixtures for new fields |

## Migration

```python
def upgrade():
    # 1. Add columns (nullable first)
    op.add_column("material_nodes",
        sa.Column("tenant_id", sa.Uuid(), nullable=True))
    op.add_column("material_nodes",
        sa.Column("learning_goal", sa.Text(), nullable=True))
    op.add_column("material_nodes",
        sa.Column("expected_knowledge", postgresql.JSONB(), nullable=True))
    op.add_column("material_nodes",
        sa.Column("expected_skills", postgresql.JSONB(), nullable=True))

    # 2. Data migration: copy tenant_id from Course for root nodes
    op.execute("""
        UPDATE material_nodes mn
        SET tenant_id = c.tenant_id
        FROM courses c
        WHERE mn.course_id = c.id
    """)

    # 3. Propagate tenant_id to children (root already has it)
    # Recursive CTE or application-level loop
    op.execute("""
        WITH RECURSIVE tree AS (
            SELECT id, tenant_id FROM material_nodes WHERE parent_id IS NULL
            UNION ALL
            SELECT mn.id, tree.tenant_id
            FROM material_nodes mn JOIN tree ON mn.parent_id = tree.id
        )
        UPDATE material_nodes mn
        SET tenant_id = tree.tenant_id
        FROM tree
        WHERE mn.id = tree.id AND mn.tenant_id IS NULL
    """)

    # 4. Make tenant_id NOT NULL
    op.alter_column("material_nodes", "tenant_id", nullable=False)

    # 5. Add FK constraint
    op.create_foreign_key(
        "fk_material_nodes_tenant", "material_nodes",
        "tenants", ["tenant_id"], ["id"])
```

**Важливо:** Це additive migration — нічого не ламає, безпечно deploy.

## Acceptance Criteria

- [ ] MaterialNode має `tenant_id` NOT NULL з FK
- [ ] MaterialNode має `learning_goal`, `expected_knowledge`, `expected_skills`
- [ ] Data migration: всі existing nodes мають tenant_id
- [ ] API response включає нові поля
- [ ] Всі тести проходять
