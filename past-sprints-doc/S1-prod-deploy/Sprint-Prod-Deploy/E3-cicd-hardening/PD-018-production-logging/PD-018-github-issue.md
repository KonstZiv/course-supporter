# PD-018: Production Logging Config

## Що

JSON structured logging для production (Docker stdout). Console format для development. Переконатись що structlog правильно налаштований для обох environments.

## Навіщо

Docker logs збирає stdout. JSON format дозволяє parsing та фільтрацію. Console format для зручності при local dev.

## Ключові рішення

- Production: JSON renderer → stdout (Docker logs збирає)
- Development: ConsoleRenderer з кольорами
- Вибір по `ENVIRONMENT` env var
- Request logging middleware вже є — перевірити format

## Acceptance Criteria

- [ ] Production logs в JSON format
- [ ] Development logs в console format
- [ ] Docker logs показує structured JSON
- [ ] Sensitive data (API keys) не логується
