# PD-014: Deep Health Check — Detail

## Реалізація

```python
# src/course_supporter/api/app.py — замінити existing health endpoint

import asyncio
from datetime import datetime, UTC


@app.get("/health")
async def health() -> dict:
    """Deep health check — verifies DB and S3 connectivity."""
    checks: dict[str, str] = {}
    overall = "ok"

    # DB check
    try:
        async with async_session() as session:
            await asyncio.wait_for(
                session.execute(text("SELECT 1")),
                timeout=5.0,
            )
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"error: {type(e).__name__}"
        overall = "degraded"

    # S3 check
    try:
        s3_client = app.state.s3_client
        await asyncio.wait_for(
            s3_client.check_connectivity(),
            timeout=5.0,
        )
        checks["s3"] = "ok"
    except Exception as e:
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

### S3Client — check method

```python
# src/course_supporter/storage/s3.py — доповнення

async def check_connectivity(self) -> None:
    """Verify S3 bucket is accessible."""
    await self._client.head_bucket(Bucket=self._bucket)
```

## Response Examples

```json
// Healthy:
{
    "status": "ok",
    "checks": {"db": "ok", "s3": "ok"},
    "timestamp": "2026-02-15T10:00:00Z"
}

// Degraded (S3 down):
{
    "status": "degraded",
    "checks": {"db": "ok", "s3": "error: ClientError"},
    "timestamp": "2026-02-15T10:00:00Z"
}
```

## Тести

Файл: `tests/unit/test_api/test_health.py` — оновити

1. **test_health_all_ok** — мокнуті DB та S3 ok → 200, status=ok
2. **test_health_db_down** — DB timeout → 503, status=degraded, db=error
3. **test_health_s3_down** — S3 error → 503, status=degraded, s3=error
4. **test_health_no_auth** — endpoint доступний без API key

Очікувана кількість тестів: **4**

## Definition of Done

- [ ] `/health` перевіряє DB та S3
- [ ] 200 для ok, 503 для degraded
- [ ] Timeout 5s per check
- [ ] `S3Client.check_connectivity()`
- [ ] 4 тести зелені
- [ ] `make check` зелений
- [ ] Документ оновлений відповідно до фінальної реалізації
