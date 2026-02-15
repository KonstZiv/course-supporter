# PD-006: Tenant-scoped Repositories — Detail

## Контекст

Після PD-002 (tenant_id columns) та PD-003 (auth middleware), потрібно гарантувати data isolation на рівні repository.

## Зміни в Repositories

### CourseRepository

```python
class CourseRepository:
    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def create(self, *, title: str, description: str | None = None) -> Course:
        course = Course(
            title=title,
            description=description,
            tenant_id=self._tenant_id,  # auto-set
        )
        self._session.add(course)
        await self._session.flush()
        return course

    async def get_by_id(self, course_id: uuid.UUID) -> Course | None:
        stmt = select(Course).where(
            Course.id == course_id,
            Course.tenant_id == self._tenant_id,  # scoped
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_with_structure(self, course_id: uuid.UUID) -> Course | None:
        stmt = (
            select(Course)
            .where(
                Course.id == course_id,
                Course.tenant_id == self._tenant_id,
            )
            .options(...)  # existing selectinload chains
        )
        ...

    async def list_all(self, *, limit: int = 50, offset: int = 0) -> list[Course]:
        stmt = (
            select(Course)
            .where(Course.tenant_id == self._tenant_id)
            .order_by(Course.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        ...
```

### LLMCallRepository

```python
class LLMCallRepository:
    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID | None = None) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def get_summary(self) -> CostSummary:
        stmt = select(...).select_from(LLMCall)
        if self._tenant_id:
            stmt = stmt.where(LLMCall.tenant_id == self._tenant_id)
        ...
```

### Log Callback

```python
# src/course_supporter/llm/logging.py — доповнення

def create_log_callback(
    session_factory: async_sessionmaker,
    tenant_id: uuid.UUID | None = None,
) -> LogCallback:
    async def callback(response, success, error_message):
        async with session_factory() as session:
            call = LLMCall(
                tenant_id=tenant_id,  # from request context
                ...
            )
            ...
```

### Endpoint Integration

```python
@router.post("/courses", status_code=201)
async def create_course(
    body: CourseCreateRequest,
    tenant: TenantContext = Depends(require_scope("prep")),
    session: AsyncSession = Depends(get_session),
) -> CourseResponse:
    repo = CourseRepository(session, tenant_id=tenant.tenant_id)
    course = await repo.create(title=body.title, description=body.description)
    ...
```

## Тести

Файл: `tests/unit/test_tenant_scoped_repos.py`

1. **test_create_course_sets_tenant_id** — course створений з правильним tenant_id
2. **test_get_by_id_wrong_tenant** — tenant A не бачить course tenant B
3. **test_list_courses_scoped** — list повертає тільки courses свого tenant
4. **test_get_with_structure_scoped** — detail scoped по tenant
5. **test_llm_call_repo_scoped** — cost report фільтрується по tenant
6. **test_llm_call_repo_no_tenant** — без tenant_id → всі записи (admin)

Очікувана кількість тестів: **6**

## Definition of Done

- [ ] `CourseRepository.__init__` приймає `tenant_id`
- [ ] Всі queries фільтрують по `tenant_id`
- [ ] CREATE auto-set `tenant_id`
- [ ] `LLMCallRepository` scoped по tenant
- [ ] Log callback записує `tenant_id`
- [ ] 6 тестів зелені
- [ ] `make check` зелений
- [ ] Документ оновлений відповідно до фінальної реалізації
