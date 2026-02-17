# PD-017: Security Hardening — Detail

## Зміни

### 1. CORS — повний набір параметрів через Settings

```python
# config.py — defaults (deny all в production):
cors_allowed_origins: list[str] = []
cors_allow_credentials: bool = True
cors_allowed_methods: list[str] = ["GET", "POST", "PUT", "DELETE"]
cors_allowed_headers: list[str] = ["Content-Type", "X-API-Key"]

# app.py — CORSMiddleware читає з settings:
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=settings.cors_allowed_methods,
    allow_headers=settings.cors_allowed_headers,
)
```

Env vars для `.env.prod`:
```
CORS_ALLOWED_ORIGINS=["https://pythoncourse.me"]
CORS_ALLOW_CREDENTIALS=true
CORS_ALLOWED_METHODS=["GET","POST","PUT","DELETE"]
CORS_ALLOWED_HEADERS=["Content-Type","X-API-Key"]
```

### 2. Debug mode — вимкнений в production

```python
app = FastAPI(
    title="Course Supporter",
    version="0.1.0",
    debug=settings.is_dev,  # False in production
)
```

### 3. Swagger UI

Залишено доступним — це B2B API, consumers потребують документацію. Захищено через API key (Swagger "Authorize" button).

### 4. Error handler — no stack traces

Вже реалізовано раніше, підтверджено тестом:

```python
@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    logger.error("unhandled_exception", exc_info=exc, path=request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
```

### 5. Nginx security headers

Вже включені з PD-010:
```nginx
add_header X-Content-Type-Options nosniff always;
add_header X-Frame-Options DENY always;
add_header X-XSS-Protection "1; mode=block" always;
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;
```

Додано в PD-017:
```nginx
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
```

## Файли

| Файл | Дія |
|------|-----|
| `src/course_supporter/config.py` | Edit — CORS settings (4 параметри) |
| `src/course_supporter/api/app.py` | Edit — `debug=settings.is_dev` + CORS params з settings |
| `deploy/nginx/course-supporter.conf` | Edit — `Referrer-Policy` header |
| `.env.example` | Edit — CORS змінні |
| `.env.prod.example` | Edit — CORS змінні |
| `tests/unit/test_api/test_security.py` | Create — 3 тести |

## Тести — `tests/unit/test_api/test_security.py`

1. **test_cors_production_restricted** — порожній `cors_allowed_origins` (default) → preflight від `http://evil.com` не повертає `access-control-allow-origin` header
2. **test_error_no_stacktrace** — `unhandled_exception_handler` з `RuntimeError("sensitive db error")` → generic "Internal server error", без sensitive даних
3. **test_debug_false_in_production** — `Settings(environment=PRODUCTION)` → `is_dev is False`; `app.debug == settings.is_dev`

Загальна кількість тестів: **3** (+ 398 total green)

## Definition of Done

- [x] CORS restricted — default `[]`, всі параметри через settings
- [x] No stack traces в responses — підтверджено тестом
- [x] Debug disabled in production — `debug=settings.is_dev`
- [x] Security headers в nginx — Referrer-Policy додано
- [x] 3 тести зелені
- [x] `make check` зелений (398 tests, lint, mypy)
- [x] Документ оновлений відповідно до фінальної реалізації
