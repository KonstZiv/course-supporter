# PD-020: Smoke Test Script

## Що

Bash скрипт для post-deploy verification: перевіряє health, auth, scope enforcement, rate limiting, basic CRUD.

## Навіщо

Автоматична верифікація після кожного deploy. Запускається в CI/CD pipeline та вручну. Якщо щось зламалось — дізнаємось за секунди, а не від клієнтів.

## Ключові рішення

- Bash script (без додаткових залежностей, тільки curl + jq)
- Exit code 0 = все ок, non-zero = fail
- Виводить чіткий report: ✅ / ❌ per check
- Потрібен API key як параметр

## Acceptance Criteria

- [ ] `scripts/smoke_test.sh` створено
- [ ] Перевіряє: health, auth (401/200), scope (403), rate limiting headers
- [ ] Чіткий вивід з ✅/❌
- [ ] Exit code відповідає результату
- [ ] Інтеграція в deploy workflow (PD-016)
