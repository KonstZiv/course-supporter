# PD-018: Production Logging Config — Detail

## Контекст

Structlog вже використовується в проєкті. Потрібно налаштувати environment-aware rendering: JSON для production (Docker stdout), console для development.

## Реалізація

### Logging config

```python
# src/course_supporter/logging.py

import logging
import sys

import structlog


def setup_logging(*, json_format: bool = False, log_level: str = "INFO") -> None:
    """Configure structlog with environment-appropriate renderer.

    Args:
        json_format: True for production (JSON lines), False for dev (console).
        log_level: Logging level string.
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        # Filter sensitive data
        _redact_sensitive_keys,
    ]

    if json_format:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    # Quiet noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("aiobotocore").setLevel(logging.WARNING)


SENSITIVE_KEYS = {"api_key", "key_hash", "password", "secret", "token", "authorization"}


def _redact_sensitive_keys(
    logger: logging.Logger,
    method_name: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """Redact values of sensitive keys in log events."""
    for key in event_dict:
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = "***REDACTED***"
    return event_dict
```

### Integration в app lifespan

```python
# src/course_supporter/api/app.py

from course_supporter.logging import setup_logging

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(
        json_format=settings.environment == "production",
        log_level=settings.log_level,
    )
    ...
```

### Config

```python
# config.py — додати якщо ще нема:
environment: str = "development"  # "development" | "production"
log_level: str = "INFO"
```

## Output Examples

### Production (JSON)

```json
{"event": "request_started", "method": "POST", "path": "/api/v1/courses", "tenant": "cs_live_a1b2", "timestamp": "2026-02-15T10:00:00Z", "level": "info"}
{"event": "llm_call_complete", "provider": "gemini", "model": "gemini-2.0-flash", "duration_ms": 1234, "tokens": 500, "timestamp": "2026-02-15T10:00:01Z", "level": "info"}
```

### Development (Console)

```
2026-02-15 10:00:00 [info] request_started method=POST path=/api/v1/courses tenant=cs_live_a1b2
2026-02-15 10:00:01 [info] llm_call_complete provider=gemini model=gemini-2.0-flash duration_ms=1234
```

## Request Logging Middleware

Перевірити/додати middleware що логує кожен request:

```python
@app.middleware("http")
async def log_requests(request: Request, call_next):
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=str(uuid4())[:8],
        method=request.method,
        path=request.url.path,
    )
    # Add tenant info if authenticated
    logger.info("request_started")
    response = await call_next(request)
    logger.info("request_completed", status=response.status_code)
    return response
```

## Sensitive Data Protection

Ніколи не логувати:
- Повний API key (тільки prefix `cs_live_a1b2`)
- Passwords, secrets, tokens
- Вміст файлів студентів

Redaction працює автоматично через `_redact_sensitive_keys` processor.

## Docker Logs

```bash
# Перегляд:
docker compose -f docker-compose.prod.yaml logs -f app

# З фільтрацією (jq):
docker compose logs app --no-log-prefix | jq 'select(.level == "error")'
```

## Тести

Файл: `tests/unit/test_logging.py`

1. **test_json_format_production** — json_format=True → JSON output
2. **test_console_format_dev** — json_format=False → console output
3. **test_sensitive_keys_redacted** — api_key в event → "***REDACTED***"
4. **test_log_level_respected** — DEBUG event з INFO level → не логується

Очікувана кількість тестів: **4**

## Definition of Done

- [ ] Environment-aware logging setup
- [ ] JSON renderer для production
- [ ] Sensitive data redaction
- [ ] Request logging middleware
- [ ] 4 тести зелені
- [ ] `make check` зелений
- [ ] Документ оновлений відповідно до фінальної реалізації
