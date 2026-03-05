# S3-009: Unified Service Registry

**Тип:** Enhancement
**Пріоритет:** Medium
**Складність:** S
**Phase:** 2

## Опис

Замінити `config/models.yaml` на `config/external_services.yaml` — unified registry для всіх зовнішніх сервісів з providers, strategies, actions.

## Вплив

- Config system (новий YAML + Pydantic model)
- LLM registry та router

## Definition of Done

- Unified config з providers/strategies/actions
- ModelRouter працює з новим форматом
