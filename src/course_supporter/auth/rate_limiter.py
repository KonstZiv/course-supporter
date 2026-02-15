"""In-memory sliding window rate limiter."""

import time
from collections import defaultdict
from threading import Lock


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

        Args:
            key: Rate limit key, e.g. "{tenant_id}:prep".
            limit: Max requests per window.

        Returns:
            (allowed, retry_after_seconds).
            If allowed: (True, 0).
            If denied: (False, seconds_until_oldest_expires).
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

        Returns:
            Number of keys cleaned up.
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
