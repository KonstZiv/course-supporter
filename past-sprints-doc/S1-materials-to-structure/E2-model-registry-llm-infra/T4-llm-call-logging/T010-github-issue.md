# S1-010: LLM Call logging

## Мета
Збереження кожного LLM-виклику в `llm_calls` з action/strategy для cost tracking.

## Що робимо
1. **Log callback** — пише LLMCall record (action, strategy, provider, tokens, cost). DB errors swallowed
2. **Setup factory** — `create_model_router()` → providers + registry + logging → router

## Контрольні точки
- [ ] Success → record з action, strategy, cost_usd, success=True
- [ ] Failure → record з success=False, error_message
- [ ] DB failure → swallowed
- [ ] `create_model_router()` → ready router
- [ ] `make check` проходить

## Залежності
- **Блокується:** S1-005 (LLMCall ORM), S1-009

## Примітка
ORM `LLMCall` потребує поля `action` та `strategy` (Alembic міграція).
