# PD-018: Production Logging Config — Detail

## Контекст

Structlog вже використовується в проєкті (S1-029). В цій задачі додано:
- Sensitive data redaction processor
- Explicit `colors=True` для development console output
- Quiet noisy libraries (uvicorn.access, aiobotocore)
- stdout як stream (для Docker logs)
- 2 нових тести (redaction + log level filtering)

## Реалізація

### Файл: `src/course_supporter/logging_config.py`

```python
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {"api_key", "key_hash", "password", "secret", "token", "authorization"}
)


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

Processor chain:
1. `merge_contextvars` — request-scoped context
2. `add_log_level` — level field
3. `PositionalArgumentsFormatter` — format %s args
4. `TimeStamper(fmt="iso")` — ISO timestamp
5. `StackInfoRenderer` — stack traces
6. `UnicodeDecoder` — bytes → str
7. `_redact_sensitive_keys` — sensitive data filtering

Renderer:
- Production (`environment == "production"`): `JSONRenderer()` → JSON lines to stdout
- Development: `ConsoleRenderer(colors=True)` → colored console output

Noisy libraries quieted:
- `uvicorn.access` → WARNING
- `aiobotocore` → WARNING

### Integration в app lifespan

```python
# src/course_supporter/api/app.py — вже інтегровано раніше
configure_logging(
    environment=str(settings.environment),
    log_level=settings.log_level,
)
```

### Config (вже існує)

```python
# config.py
environment: Environment = Environment.DEVELOPMENT  # StrEnum
log_level: str = "DEBUG"
```

### Request Logging Middleware (вже існує)

```python
# src/course_supporter/api/middleware.py — RequestLoggingMiddleware
# Логує: method, path, status_code, latency_ms
# Пропускає: /health, /docs, /openapi.json, /redoc
```

## Output Examples

### Production (JSON)

```json
{"event": "http_request", "method": "POST", "path": "/api/v1/courses", "status_code": 201, "latency_ms": 45, "timestamp": "2026-02-17T10:00:00Z", "level": "info"}
{"event": "llm_call_complete", "provider": "gemini", "model": "gemini-2.5-flash", "duration_ms": 1234, "tokens": 500, "timestamp": "2026-02-17T10:00:01Z", "level": "info"}
```

### Development (Console, з кольорами)

```
2026-02-17T10:00:00Z [info] http_request method=POST path=/api/v1/courses status_code=201 latency_ms=45
2026-02-17T10:00:01Z [info] llm_call_complete provider=gemini model=gemini-2.5-flash duration_ms=1234
```

## Sensitive Data Protection

Автоматична redaction через `_redact_sensitive_keys` processor:
- `api_key`, `key_hash`, `password`, `secret`, `token`, `authorization`
- Значення замінюється на `***REDACTED***`

## Docker Logs

```bash
# Перегляд:
docker compose -f docker-compose.prod.yaml logs -f app

# З фільтрацією (jq):
docker compose logs app --no-log-prefix | jq 'select(.level == "error")'
```

## Тести

Файл: `tests/unit/test_logging_config.py` — **9 тестів**

### TestConfigureLogging (6 тестів)
1. **test_configure_production_json** — production → valid JSON output
2. **test_configure_development_console** — development → human-readable console
3. **test_configure_sets_log_level** — root logger level matches config
4. **test_configure_includes_timestamp** — ISO timestamp in JSON output
5. **test_sensitive_keys_redacted** — api_key/token → `***REDACTED***`
6. **test_log_level_respected** — DEBUG event at INFO level → not emitted

### TestRequestLoggingMiddleware (3 тести)
7. **test_middleware_logs_request** — logs method, path, status_code, latency_ms
8. **test_middleware_skips_health** — /health not logged
9. **test_middleware_skips_docs** — /docs not logged

## Definition of Done

- [x] Environment-aware logging setup
- [x] JSON renderer для production
- [x] Console renderer з кольорами для development
- [x] Sensitive data redaction (`_redact_sensitive_keys` processor)
- [x] Noisy libraries quieted (uvicorn.access, aiobotocore)
- [x] Request logging middleware
- [x] stdout як output stream (Docker logs)
- [x] 9 тестів зелені (6 config + 3 middleware)
- [x] `make check` зелений (400 тестів)
- [x] Документ оновлений відповідно до фінальної реалізації
