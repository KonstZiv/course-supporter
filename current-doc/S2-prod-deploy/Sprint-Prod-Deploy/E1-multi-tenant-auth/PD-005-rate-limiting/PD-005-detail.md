# PD-005: Rate Limiting Middleware — Detail ✅

## Контекст

Після PD-003 та PD-004, кожен запит має `TenantContext` з rate_limit_prep та rate_limit_check. Потрібно enforce ці ліміти.

## Алгоритм: Sliding Window Counter

```
Window = 60 seconds
Key = (tenant_id, scope)

Для кожного запиту:
1. Видалити timestamps старіші за window
2. Якщо len(timestamps) >= limit → 429
3. Інакше → додати поточний timestamp, пропустити
```

## Реалізація

### InMemoryRateLimiter

`src/course_supporter/auth/rate_limiter.py`:

```python
class InMemoryRateLimiter:
    """Sliding window rate limiter.

    Thread-safe via Lock. Single-instance only.
    For multi-instance deployments: replace with Redis backend.
    """

    def __init__(self, window_seconds: int = 60) -> None:
        self._window = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def check(self, key: str, limit: int) -> tuple[bool, int]:
        """Check if request is allowed.

        Returns:
            (allowed, retry_after_seconds).
        """
        ...

    def cleanup(self) -> int:
        """Remove all expired entries. Call periodically.

        Returns:
            Number of keys cleaned up.
        """
        ...
```

### Інтеграція з require_scope

Rate limiting вбудований в `require_scope()` в `auth/scopes.py`:

```python
# Global rate limiter instance (single-process; replace with Redis for scaling)
rate_limiter = InMemoryRateLimiter(window_seconds=60)


def require_scope(
    *required_scopes: str,
) -> Callable[..., Coroutine[Any, Any, TenantContext]]:
    async def _check_scope(
        tenant: TenantContext = _tenant_dep,
    ) -> TenantContext:
        # 1. Scope check — next() returns matched scope
        matched_scope = next((s for s in required_scopes if s in tenant.scopes), None)
        if matched_scope is None:
            raise HTTPException(status_code=403, ...)

        # 2. Rate limit check — explicit if/elif per scope
        if matched_scope == "prep":
            limit = tenant.rate_limit_prep
        elif matched_scope == "check":
            limit = tenant.rate_limit_check
        else:
            limit = tenant.rate_limit_check  # future scopes fallback
        key = f"{tenant.tenant_id}:{matched_scope}"
        allowed, retry_after = rate_limiter.check(key, limit)

        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

        return tenant

    return _check_scope
```

> **Ключові рішення:**
> - **Explicit `if/elif`** замість dict lookup — чіткіший mapping scope → limit.
> - **`rate_limiter`** — module-level global instance, доступний для cleanup з `app.py`.
> - **`next()` для scope matching** — повертає конкретний matched scope для вибору rate limit.

### Periodic Cleanup

`src/course_supporter/api/app.py`:

```python
CLEANUP_INTERVAL_SECONDS = 300


async def _cleanup_loop(limiter: InMemoryRateLimiter) -> None:
    """Periodic cleanup of expired rate limit entries."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            cleaned = await asyncio.to_thread(limiter.cleanup)
            if cleaned:
                logger.debug("rate_limiter_cleanup", keys_removed=cleaned)
        except Exception:
            logger.exception("rate_limiter_cleanup_error")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    ...
    cleanup_task = asyncio.create_task(_cleanup_loop(rate_limiter))
    ...
    yield
    cleanup_task.cancel()
```

> **`asyncio.to_thread`** — cleanup використовує `threading.Lock`, тому запускається в thread pool щоб не блокувати event loop. Error handling в loop — `except Exception` + `logger.exception` щоб не зупиняти task на неочікуваних помилках.

## Тести

Файл: `tests/unit/test_rate_limiter.py` — **7 тестів**

1. `test_allows_under_limit` — 5 req з limit=10 → всі allowed
2. `test_blocks_over_limit` — 11 req з limit=10 → 11-й blocked
3. `test_retry_after_positive` — blocked request → retry_after ≥ 1
4. `test_window_expires` — після window_seconds, mock `time.monotonic`, старі req не враховуються
5. `test_different_keys_independent` — tenant_a і tenant_b мають окремі ліміти
6. `test_cleanup_removes_expired` — cleanup видаляє пусті ключі, перевіряє `limiter._requests`
7. `test_429_response_in_api` — HTTP test: rate_limit_prep=2, 3-й запит → 429 з `Retry-After` header

## Definition of Done

- [x] `InMemoryRateLimiter` з sliding window
- [x] Інтеграція з `require_scope()` — explicit `if/elif` для scope limits
- [x] 429 з `Retry-After` header
- [x] Periodic cleanup в lifespan з `asyncio.to_thread` та error handling
- [x] 7 тестів зелені
- [x] `make check` зелений
