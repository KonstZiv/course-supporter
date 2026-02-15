# PD-004: Service Scope Enforcement

## Що

Перевірка scope на рівні endpoint: tenant з scope `prep` не може викликати `check` endpoints, і навпаки.

## Навіщо

Два сервіси білінгуються окремо. Tenant може мати доступ тільки до одного сервісу. Scope enforcement запобігає несанкціонованому доступу та спрощує білінг.

## Ключові рішення

- Decorator/dependency `require_scope("prep")` на рівні endpoint
- Scopes: `prep` (курс підготовка), `check` (перевірка ДЗ)
- 403 Forbidden при невідповідності scope
- Деякі endpoints доступні для обох scopes (lessons, reports)

## Acceptance Criteria

- [ ] `require_scope()` dependency
- [ ] Prep endpoints захищені scope `prep`
- [ ] Запит зі scope `check` до prep endpoint → 403
- [ ] Endpoints зі спільним доступом працюють з обома scopes
- [ ] Тести на scope enforcement
