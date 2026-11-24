"""Open courses (O-1) without a database: the key, the links, the mask, the
confirm email in both languages, and the Redis rate limiter's counting and its
fail-open path."""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from structlog.testing import capture_logs

from course_supporter.auth.rate_limits import (
    RateLimitedError,
    RedisRateLimiter,
    counter_key,
)
from course_supporter.config import RateLimit
from course_supporter.open_courses.links import (
    SIGNUP_TTL,
    enrollment_url,
    generate_enroll_key,
    mask_email,
    signup_confirm_url,
    signup_ttl_display,
)
from course_supporter.services.email_text import render_email


class TestKeyAndLinks:
    def test_the_key_has_at_least_128_bits_and_is_url_safe(self) -> None:
        keys = {generate_enroll_key() for _ in range(50)}
        assert len(keys) == 50
        for key in keys:
            assert len(key) >= 22  # 16 bytes of urlsafe base64
            assert key.replace("-", "").replace("_", "").isalnum()

    def test_links_carry_the_tenant_and_the_key(self) -> None:
        tenant = uuid.uuid4()
        assert enrollment_url(tenant, "k3y").endswith(f"/{tenant}/join/k3y")
        assert signup_confirm_url(tenant, "t0k").endswith(
            f"/{tenant}/join/confirm?token=t0k"
        )

    def test_the_ttl_phrase_matches_the_ttl(self) -> None:
        assert SIGNUP_TTL.days == 7
        assert signup_ttl_display("eng") == "7 days"
        assert signup_ttl_display(None) == "7 днів"


class TestMask:
    @pytest.mark.parametrize(
        ("email", "masked"),
        [
            ("alice@example.com", "a***@e***.com"),
            ("b@mail.example.co.uk", "b***@m***.uk"),
            ("c@localhost", "c***@l***"),
        ],
    )
    def test_mask(self, email: str, masked: str) -> None:
        assert mask_email(email) == masked


class TestConfirmEmail:
    _CONTEXT: ClassVar[dict[str, str]] = {
        "course_title": "Python",
        "author_name": "School",
        "confirm_url": "https://p/t/join/confirm?token=x",
        "author_login_url": "https://p/t/login",
        "shared_login_url": "https://p/login",
    }

    def test_ukrainian(self) -> None:
        mail = render_email(
            "portal_signup_confirm", language=None, ttl="7 днів", **self._CONTEXT
        )
        assert "Python" in mail.subject
        for part in (
            "https://p/t/join/confirm?token=x",
            "протягом 7 днів",
            "запис буде скасовано",
            "Якщо у вас уже є вхід у цього автора, підтверджувати не обов'язково",
            "https://p/t/login",
            "https://p/login",
            "School",
        ):
            assert part in mail.body

    def test_english(self) -> None:
        mail = render_email(
            "portal_signup_confirm", language="eng", ttl="7 days", **self._CONTEXT
        )
        assert mail.subject.startswith("Confirm your enrollment")
        assert "within 7 days" in mail.body
        assert "you do not need to confirm" in mail.body

    @pytest.mark.parametrize("language", [None, "eng"])
    def test_no_avoided_words(self, language: str | None) -> None:
        mail = render_email(
            "portal_signup_confirm", language=language, ttl="7", **self._CONTEXT
        )
        text = (mail.subject + mail.body).lower()
        for word in (
            "баланс",
            "гаманець",
            "поповнення",
            "купити курс",
            "маркетплейс",
            "balance",
            "wallet",
            "top up",
            "marketplace",
        ):
            assert word not in text


class _Pipe:
    """A MULTI pipeline over :class:`_FakeRedis`."""

    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def __aenter__(self) -> _Pipe:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def set(self, *args: Any, **kwargs: Any) -> None:
        self._ops.append(("set", args, kwargs))

    def incr(self, *args: Any) -> None:
        self._ops.append(("incr", args, {}))

    def ttl(self, *args: Any) -> None:
        self._ops.append(("ttl", args, {}))

    async def execute(self) -> list[Any]:
        if self._redis.broken:
            raise RedisConnectionError("down")
        out: list[Any] = []
        for op, args, kwargs in self._ops:
            key = args[0]
            if op == "set":
                if key not in self._redis.values:
                    self._redis.values[key] = 0
                    self._redis.ttls[key] = kwargs["ex"]
                out.append(True)
            elif op == "incr":
                self._redis.values[key] += 1
                out.append(self._redis.values[key])
            else:
                out.append(self._redis.ttls[key])
        return out


class _FakeRedis:
    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> _Pipe:
        return _Pipe(self)


class TestRedisRateLimiter:
    async def test_over_the_limit_raises_with_the_seconds_left(self) -> None:
        limiter = RedisRateLimiter(_FakeRedis())  # type: ignore[arg-type]
        rule = RateLimit(limit=2, window_s=60)
        await limiter.hit("x", "1.2.3.4", rule)
        await limiter.hit("x", "1.2.3.4", rule)
        with pytest.raises(RateLimitedError) as caught:
            await limiter.hit("x", "1.2.3.4", rule)
        assert caught.value.retry_after == 60
        await limiter.hit("x", "5.6.7.8", rule)  # another subject: its own count

    async def test_the_key_holds_a_hash_not_the_subject(self) -> None:
        key = counter_key("login_by_email_email", "alice@example.com")
        assert "alice" not in key and "example" not in key
        assert key.startswith("rl:login_by_email_email:")

    async def test_redis_down_lets_the_request_through_with_a_warning(self) -> None:
        limiter = RedisRateLimiter(_FakeRedis(broken=True))  # type: ignore[arg-type]
        with capture_logs() as logs:
            await limiter.hit("x", "1.2.3.4", RateLimit(limit=1, window_s=60))
        [line] = [e for e in logs if e["event"] == "rate_limit_unavailable"]
        assert line["log_level"] == "warning"
        assert line["error"] == "ConnectionError"

    async def test_no_redis_lets_the_request_through_with_a_warning(self) -> None:
        with capture_logs() as logs:
            await RedisRateLimiter(None).hit("x", "s", RateLimit(limit=1, window_s=1))
        assert [e["log_level"] for e in logs] == ["warning"]
