# PD-020: Smoke Test Script — Detail

## Контекст

Фінальна задача спрінту. Bash скрипт для post-deploy verification — запускається після deploy (в CI/CD та вручну) і перевіряє що всі критичні функції працюють.

## Реалізація

### Файл: `scripts/smoke_test.sh`

Bash скрипт (curl + jq), 10 перевірок у 5 секціях:

1. **Health Check** (3 checks)
   - `GET /health` → 200
   - DB connectivity → ok
   - S3 connectivity → ok

2. **Authentication** (3 checks)
   - Без ключа → 401
   - Невалідний ключ → 401
   - Валідний ключ → 200

3. **Swagger UI** (1 check)
   - `GET /docs` → 200

4. **Security Headers** (2 checks)
   - `X-Content-Type-Options: nosniff`
   - `X-Frame-Options` present

5. **Response Format** (1 check)
   - Response is valid JSON

### Usage

```bash
# Зовнішній (з машини з доступом до API):
./scripts/smoke_test.sh https://api.pythoncourse.me cs_live_abc123...

# З VPS (через localhost):
./scripts/smoke_test.sh http://localhost:8000 cs_live_abc123...
```

### Exit codes

- `0` — всі перевірки пройшли
- `1` — є failures

### CI/CD інтеграція

Додано step до `.github/workflows/deploy.yml`:
- Запускається після deploy + health check
- Conditional: тільки якщо `SMOKE_TEST_API_KEY` secret задано
- Потрібен GitHub Secret: `SMOKE_TEST_API_KEY` — ключ tenant з scope prep

## Definition of Done

- [x] `scripts/smoke_test.sh` створено та executable
- [x] Перевіряє: health (DB + S3), auth (401/200), headers, JSON format
- [x] Чіткий вивід з emoji per check
- [x] Exit code 0/1 відповідає результату
- [x] Інтегровано в deploy workflow (conditional on secret)
- [x] `make check` зелений (400 тестів)
- [x] Документ оновлений відповідно до фінальної реалізації
