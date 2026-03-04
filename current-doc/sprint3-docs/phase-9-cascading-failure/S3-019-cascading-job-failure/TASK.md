# S3-019: Cascading Job Failure Propagation

**Phase:** 9 (Cascading Job Failure)
**Складність:** M
**Статус:** PENDING
**Залежність:** S3-004 (Job result cleanup)

## Контекст

`depends_on` — flat JSONB list[str] of Job UUIDs. Currently no failure propagation:
```
Job A depends_on: [B, C, D]
Job C depends_on: [E, F]
If E fails → C should fail → A should fail
Currently: A waits indefinitely.
```

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `src/course_supporter/storage/job_repository.py` | `propagate_failure(job_id)` — recursive failure propagation |
| `src/course_supporter/api/tasks.py` | Call `propagate_failure()` in error handler |
| `tests/unit/test_job_repository.py` | Tests for propagation |
| `tests/unit/test_cascading_failure.py` | НОВИЙ — multi-level propagation tests |

## Деталі реалізації

### 1. JobRepository.propagate_failure()

```python
async def propagate_failure(self, failed_job_id: uuid.UUID) -> list[uuid.UUID]:
    """Propagate failure to all dependent jobs recursively.

    Returns list of newly failed job IDs.
    """
    failed_ids = []
    # Find jobs where depends_on contains failed_job_id
    # JSONB contains: depends_on @> '["<uuid>"]'
    dependents = await self._find_dependents(failed_job_id)

    for job in dependents:
        if job.status in ("queued", "active"):
            job.status = "failed"
            job.error_message = f"Dependency {failed_job_id} failed"
            job.completed_at = datetime.now(UTC)
            failed_ids.append(job.id)
            # Recursive propagation
            failed_ids.extend(await self.propagate_failure(job.id))

    await self._session.flush()
    return failed_ids
```

### 2. JSONB Query

```python
async def _find_dependents(self, job_id: uuid.UUID) -> list[Job]:
    """Find all jobs that depend on given job_id."""
    stmt = select(Job).where(
        Job.depends_on.contains([str(job_id)])
    )
    result = await self._session.execute(stmt)
    return list(result.scalars().all())
```

### 3. Integration in tasks.py

В error handler `arq_ingest_material` та `arq_generate_structure`:
```python
async def on_failure(job_id, error):
    async with session_factory() as session:
        repo = JobRepository(session)
        await repo.fail_job(job_id, str(error))
        await repo.propagate_failure(job_id)
        await session.commit()
```

### 4. Design Decision

**`failed` not `cancelled`:** dependent jobs get status `failed` because the dependency system promised execution but couldn't deliver. `cancelled` would imply user action.

## Тести

1. **Single level:** A depends on B. B fails → A fails.
2. **Multi level:** A → B → C. C fails → B fails → A fails.
3. **Diamond:** A → [B, C]. B → D. C → D. D fails → B,C fail → A fails.
4. **Already completed:** A depends on B (completed) and C (failed). Only new failure propagates.
5. **Idempotency:** propagate_failure on already-failed job → no changes.

## Acceptance Criteria

- [ ] `propagate_failure()` працює рекурсивно
- [ ] JSONB query знаходить dependents
- [ ] Error message вказує на конкретну failed dependency
- [ ] Інтегровано в task error handlers
- [ ] 5+ тестів покривають edge cases
