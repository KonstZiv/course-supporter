# PD-005: Rate Limiting Middleware

## Що

Per-tenant, per-scope rate limiting з sliding window алгоритмом. In-memory реалізація для single instance.

## Навіщо

Захист від зловживань та основа для fair use policy. Rate limits різні для prep (рідкісні тяжкі запити) та check (часті легкі запити).

## Ключові рішення

- Sliding window в пам'яті (dict з timestamps)
- Per-scope ліміти з `TenantContext.rate_limit_prep` / `rate_limit_check`
- Перевищення → 429 Too Many Requests з `Retry-After` header
- TTL cleanup для запобігання memory leak
- Defaults: prep=60 req/min, check=300 req/min

## Acceptance Criteria

- [ ] Rate limiter з sliding window
- [ ] 429 з Retry-After header при перевищенні
- [ ] Окремі ліміти для prep та check scopes
- [ ] Memory cleanup (TTL на старих записах)
- [ ] Тести на rate limiting logic
