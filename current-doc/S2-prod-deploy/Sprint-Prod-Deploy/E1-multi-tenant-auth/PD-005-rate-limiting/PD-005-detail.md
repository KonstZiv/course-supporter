# PD-005: Rate Limiting Middleware — Detail

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

```python
# src/course_supporter/auth/rate_limiter.py

import time
from collections import defaultdict
from threading import Lock


class InMemoryRateLimiter:
    """Sliding window rate limiter.

    Thread-safe via Lock. Single-instance only.
    For multi-instance: replace with Redis backend.
    """

    def __init__(self, window_seconds: int = 60) -> None:
        self._window = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def check(self, key: str, limit: int) -> tuple[bool, int]:
        """Check if request is allowed.

        Args:
            key: Rate limit key, e.g. "{tenant_id}:prep"
            limit: Max requests per window

        Returns:
            (allowed, retry_after_seconds)
            If allowed: (True, 0)
            If denied: (False, seconds_until_oldest_expires)
        """
        now = time.monotonic()
        cutoff = now - self._window

        with self._lock:
            timestamps = self._requests[key]
            # Remove expired entries
            self._requests[key] = [t for t in timestamps if t > cutoff]
            timestamps = self._requests[key]

            if len(timestamps) >= limit:
                retry_after = int(timestamps[0] - cutoff) + 1
                return False, max(retry_after, 1)

            timestamps.append(now)
            return True, 0

    def cleanup(self) -> int:
        """Remove all expired entries. Call periodically.

        Returns number of keys cleaned up.
        """
        now = time.monotonic()
        cutoff = now - self._window
        cleaned = 0

        with self._lock:
            empty_keys = []
            for key, timestamps in self._requests.items():
                self._requests[key] = [t for t in timestamps if t > cutoff]
                if not self._requests[key]:
                    empty_keys.append(key)
            for key in empty_keys:
                del self._requests[key]
                cleaned += 1

        return cleaned
```

### FastAPI Integration

```python
# src/course_supporter/auth/scopes.py — доповнення require_scope

# Global rate limiter instance (created once at import)
_rate_limiter = InMemoryRateLimiter(window_seconds=60)


def require_scope(*required_scopes: str):
    async def _check_scope(
        tenant: TenantContext = Depends(get_current_tenant),
    ) -> TenantContext:
        # 1. Scope check (existing)
        matched_scope = None
        for s in required_scopes:
            if s in tenant.scopes:
                matched_scope = s
                break
        if matched_scope is None:
            raise HTTPException(status_code=403, ...)

        # 2. Rate limit check
        limit = (
            tenant.rate_limit_prep if matched_scope == "prep"
            else tenant.rate_limit_check
        )
        key = f"{tenant.tenant_id}:{matched_scope}"
        allowed, retry_after = _rate_limiter.check(key, limit)

        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

        return tenant

    return _check_scope
```

### Periodic Cleanup

В lifespan app:

```python
async def _cleanup_loop() -> None:
    """Periodic cleanup of expired rate limit entries."""
    while True:
        await asyncio.sleep(300)  # every 5 minutes
        _rate_limiter.cleanup()

@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_task = asyncio.create_task(_cleanup_loop())
    ...
    yield
    cleanup_task.cancel()
```

## Тести

Файл: `tests/unit/test_rate_limiter.py`

1. **test_allows_under_limit** — 5 req з limit=10 → всі allowed
2. **test_blocks_over_limit** — 11 req з limit=10 → 11-й blocked
3. **test_retry_after_positive** — blocked request → retry_after > 0
4. **test_window_expires** — після window_seconds старі req не враховуються
5. **test_different_keys_independent** — tenant_a і tenant_b мають окремі ліміти
6. **test_cleanup_removes_expired** — cleanup видаляє пусті ключі
7. **test_429_response_in_api** — HTTP test: перевищення ліміту → 429 з Retry-After

Очікувана кількість тестів: **7**

## Definition of Done

- [ ] `InMemoryRateLimiter` з sliding window
- [ ] Інтеграція з `require_scope()`
- [ ] 429 з Retry-After header
- [ ] Periodic cleanup в lifespan
- [ ] 7 тестів зелені
- [ ] `make check` зелений
- [ ] Документ оновлений відповідно до фінальної реалізації
