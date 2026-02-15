# PD-006: Tenant-scoped Repositories — Detail ✅

## Контекст

Після PD-002 (tenant_id columns) та PD-003 (auth middleware), потрібно гарантувати data isolation на рівні repository.

## Зміни в Repositories

### CourseRepository

```python
class CourseRepository:
    """Tenant-scoped repository for Course CRUD operations.

    All queries are automatically filtered by tenant_id to ensure
    data isolation between tenants.
    """

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def create(self, *, title: str, description: str | None = None) -> Course:
        """Create a new course for the current tenant."""
        course = Course(tenant_id=self._tenant_id, title=title, description=description)
        self._session.add(course)
        await self._session.flush()
        return course

    async def get_by_id(self, course_id: uuid.UUID) -> Course | None:
        """Get course by primary key, scoped to current tenant."""
        stmt = select(Course).where(
            Course.id == course_id,
            Course.tenant_id == self._tenant_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_with_structure(self, course_id: uuid.UUID) -> Course | None:
        """Get course with all nested structure eagerly loaded, scoped to tenant."""
        stmt = (
            select(Course)
            .where(
                Course.id == course_id,
                Course.tenant_id == self._tenant_id,
            )
            .options(
                selectinload(Course.source_materials),
                selectinload(Course.modules)
                .selectinload(Module.lessons)
                .selectinload(Lesson.concepts),
                selectinload(Course.modules)
                .selectinload(Module.lessons)
                .selectinload(Lesson.exercises),
            )
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self, *, limit: int = 50, offset: int = 0) -> list[Course]:
        """List courses for current tenant, ordered by creation date."""
        stmt = (
            select(Course)
            .where(Course.tenant_id == self._tenant_id)
            .order_by(Course.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
```

> **Ключові зміни:**
> - `__init__` тепер приймає `tenant_id: uuid.UUID` (обов'язковий параметр).
> - `create()` auto-set `tenant_id` з конструктора замість параметра.
> - `get_by_id()` — `select().where()` замість `session.get()`, бо потрібен tenant filter.
> - Всі queries фільтрують по `Course.tenant_id == self._tenant_id`.

### LLMCallRepository

```python
class LLMCallRepository:
    """Repository for LLM call analytics and cost reporting.

    Optionally scoped by tenant_id. When tenant_id is provided,
    all queries filter by it. When None, returns all records.
    """

    def __init__(
        self, session: AsyncSession, tenant_id: uuid.UUID | None = None
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def get_summary(self) -> CostSummary:
        stmt = select(...).select_from(LLMCall)
        if self._tenant_id is not None:
            stmt = stmt.where(LLMCall.tenant_id == self._tenant_id)
        ...

    async def _grouped_query(self, group_column: ...) -> list[GroupedCost]:
        stmt = select(...).select_from(LLMCall).group_by(group_column)
        if self._tenant_id is not None:
            stmt = stmt.where(LLMCall.tenant_id == self._tenant_id)
        ...
```

> **`is not None` перевірка** — `if self._tenant_id is not None` замість `if self._tenant_id`, бо UUID може бути truthy/falsy-нейтральним.

### LLMCall.tenant_id — nullable

В рамках PD-006 виявлено, що global `ModelRouter` створюється при старті app без tenant context. Тому `LLMCall.tenant_id` зроблено **nullable**:

```python
# storage/orm.py
class LLMCall(Base):
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
    )
    tenant: Mapped["Tenant | None"] = relationship()
```

Окрема міграція: `d3d44a540129_make_llm_calls_tenant_id_nullable.py`.

### Log Callback

```python
# src/course_supporter/llm/logging.py

def create_log_callback(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID | None = None,
) -> LogCallback:
    async def _log_to_db(response, success, error_message):
        record = LLMCall(
            tenant_id=tenant_id,  # from request context or None for global
            ...
        )
        ...

    return _log_to_db
```

### Endpoint Integration

```python
# src/course_supporter/api/routes/courses.py

@router.post("/courses", status_code=201)
async def create_course(
    body: CourseCreateRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> CourseResponse:
    repo = CourseRepository(session, tenant.tenant_id)
    course = await repo.create(title=body.title, description=body.description)
    ...


# src/course_supporter/api/routes/reports.py

@router.get("/reports/cost")
async def cost_report(
    tenant: SharedDep,
    session: SessionDep,
) -> CostReport:
    repo = LLMCallRepository(session, tenant.tenant_id)
    return await repo.get_full_report()
```

## Alembic Migration

Файл: `migrations/versions/d3d44a540129_make_llm_calls_tenant_id_nullable.py`

```python
def upgrade() -> None:
    op.alter_column('llm_calls', 'tenant_id', existing_type=sa.UUID(), nullable=True)

def downgrade() -> None:
    op.alter_column('llm_calls', 'tenant_id', existing_type=sa.UUID(), nullable=False)
```

## Тести

Файл: `tests/unit/test_tenant_scoped_repos.py` — **6 тестів**

1. `test_create_course_sets_tenant_id` — `create()` auto-sets tenant_id з конструктора
2. `test_get_by_id_wrong_tenant` — tenant A не бачить course tenant B → `None`
3. `test_list_courses_scoped` — `list_all()` повертає тільки courses свого tenant
4. `test_get_with_structure_scoped` — detail scoped по tenant
5. `test_llm_call_repo_scoped` — `get_summary()` фільтрується по tenant
6. `test_llm_call_repo_no_tenant` — без tenant_id → всі записи (admin/global)

## Definition of Done

- [x] `CourseRepository.__init__` приймає `tenant_id`
- [x] Всі queries фільтрують по `tenant_id`
- [x] `create()` auto-set `tenant_id` з конструктора
- [x] `get_by_id()` → `select().where()` замість `session.get()`
- [x] `LLMCallRepository` scoped по tenant (optional)
- [x] `LLMCall.tenant_id` nullable (міграція `d3d44a540129`)
- [x] Log callback записує `tenant_id`
- [x] 6 тестів зелені
- [x] `make check` зелений
