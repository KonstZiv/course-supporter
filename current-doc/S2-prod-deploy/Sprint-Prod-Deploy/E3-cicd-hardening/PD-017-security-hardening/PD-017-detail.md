# PD-017: Security Hardening — Detail

## Зміни

### 1. CORS

```python
# config.py — default для production:
cors_allowed_origins: list[str] = []  # empty = deny all

# .env.prod:
CORS_ALLOWED_ORIGINS=["https://pythoncourse.me"]
```

### 2. Swagger UI

Залишити доступним — це B2B API, consumers потребують документацію. Але захистити через API key (вже є — Swagger "Authorize" button).

### 3. Error handler — no stack traces

```python
@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    logger.error("unhandled_exception", exc_info=exc, path=request.url.path)
    # Never expose internal details
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
```

Вже реалізовано — перевірити що не змінилось.

### 4. Environment check

```python
# app.py — при створенні FastAPI:
app = FastAPI(
    title="Course Supporter",
    version="0.1.0",
    # Disable debug features in production
    debug=settings.is_dev,
)
```

### 5. Additional headers (nginx level — PD-010)

```nginx
# Вже включені в PD-010:
add_header X-Content-Type-Options nosniff always;
add_header X-Frame-Options DENY always;
add_header X-XSS-Protection "1; mode=block" always;

# Додати:
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
```

## Тести

1. **test_cors_production_restricted** — production config → тільки allowed origins
2. **test_error_no_stacktrace** — unhandled exception → generic message
3. **test_debug_false_in_production** — production env → debug=False

Очікувана кількість тестів: **3**

## Definition of Done

- [ ] CORS restricted
- [ ] No stack traces в responses
- [ ] Debug disabled in production
- [ ] Security headers в nginx
- [ ] 3 тести зелені
- [ ] `make check` зелений
- [ ] Документ оновлений відповідно до фінальної реалізації
