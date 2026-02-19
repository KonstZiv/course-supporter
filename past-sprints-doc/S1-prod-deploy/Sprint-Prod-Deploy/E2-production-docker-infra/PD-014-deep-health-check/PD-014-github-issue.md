# PD-014: Deep Health Check

## Що

Розширити `/health` endpoint для перевірки DB connectivity та S3 reachability.

## Навіщо

Поточний `/health` повертає статичний `{"status": "ok"}`. Для production потрібно знати чи DB та S3 дійсно доступні — для UptimeRobot alerts та load balancer checks.

## Ключові рішення

- DB: `SELECT 1` через async session
- S3: `head_bucket()` call
- Кожен компонент окремо: можна бачити що саме впало
- Timeout 5s на кожну перевірку
- Загальний status: "ok" якщо все працює, "degraded" якщо частково

## Acceptance Criteria

- [ ] `/health` перевіряє DB та S3
- [ ] Відповідь містить status кожного компонента
- [ ] Timeout 5s per check
- [ ] Тести на healthy/degraded scenarios
