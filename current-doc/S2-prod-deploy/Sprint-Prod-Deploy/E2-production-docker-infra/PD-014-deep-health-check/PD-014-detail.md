# PD-014: Deep Health Check — Detail

## Реалізація

### `/health` endpoint — `src/course_supporter/api/app.py`

Замінено статичний `{"status": "ok"}` на deep health check з перевіркою DB та S3.

```python
HEALTH_CHECK_TIMEOUT = 5.0


@app.get("/health")
async def health() -> JSONResponse:
    """Deep health check — verifies DB and S3 connectivity."""
    checks: dict[str, str] = {}
    overall = "ok"

    # DB check — SELECT 1 через async_session
    try:
        async with async_session() as session:
            await asyncio.wait_for(
                session.execute(text("SELECT 1")),
                timeout=HEALTH_CHECK_TIMEOUT,
            )
        checks["db"] = "ok"
    except (TimeoutError, OperationalError, SQLAlchemyError) as e:
        log.warning("health_check_db_error", error=type(e).__name__)
        checks["db"] = f"error: {type(e).__name__}"
        overall = "degraded"
    except Exception as e:
        log.error("health_check_db_unexpected", error=type(e).__name__)
        checks["db"] = f"error: {type(e).__name__}"
        overall = "degraded"

    # S3 check — head_bucket через S3Client
    try:
        s3_client = app.state.s3_client
        await asyncio.wait_for(
            s3_client.check_connectivity(),
            timeout=HEALTH_CHECK_TIMEOUT,
        )
        checks["s3"] = "ok"
    except (TimeoutError, ClientError) as e:
        log.warning("health_check_s3_error", error=type(e).__name__)
        checks["s3"] = f"error: {type(e).__name__}"
        overall = "degraded"
    except Exception as e:
        log.error("health_check_s3_unexpected", error=type(e).__name__)
        checks["s3"] = f"error: {type(e).__name__}"
        overall = "degraded"

    status_code = 200 if overall == "ok" else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall,
            "checks": checks,
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )
```

### `S3Client.check_connectivity()` — `src/course_supporter/storage/s3.py`

```python
async def check_connectivity(self) -> None:
    """Verify S3 bucket is accessible."""
    if self._client is None:
        msg = "S3Client not initialized. Use 'async with S3Client(...)'"
        raise RuntimeError(msg)

    await self._client.head_bucket(Bucket=self._bucket)
```

## Response Examples

```json
// Healthy — 200:
{
    "status": "ok",
    "checks": {"db": "ok", "s3": "ok"},
    "timestamp": "2026-02-16T10:00:00+00:00"
}

// Degraded (S3 down) — 503:
{
    "status": "degraded",
    "checks": {"db": "ok", "s3": "error: ClientError"},
    "timestamp": "2026-02-16T10:00:00+00:00"
}
```

## Тести

Файл: `tests/unit/test_api/test_health.py`

1. **test_health_all_ok** — мокнуті DB та S3 ok → 200, status=ok, checks=ok, timestamp present
2. **test_health_db_down** — DB TimeoutError → 503, status=degraded, db=error, s3=ok
3. **test_health_s3_down** — S3 ConnectionError → 503, status=degraded, db=ok, s3=error
4. **test_health_no_auth** — endpoint доступний без API key (без dependency overrides)

Додатково оновлено `tests/unit/test_auth_middleware.py::test_health_no_auth` — мокнуті DB/S3 для deep check.

Загальна кількість тестів: **396** (було 393 + 3 нових)

## Definition of Done

- [x] `/health` перевіряє DB та S3
- [x] 200 для ok, 503 для degraded
- [x] Timeout 5s per check (`HEALTH_CHECK_TIMEOUT` constant)
- [x] `S3Client.check_connectivity()` з RuntimeError guard
- [x] 4 тести зелені
- [x] `make check` зелений (ruff + mypy + 396 tests)
- [x] Документ оновлений відповідно до фінальної реалізації
