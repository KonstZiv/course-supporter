# Phase 2: ExternalServiceCall + Config

**Складність:** M (Medium)
**Залежності:** Немає
**Задачі:** S3-008, S3-009
**PR:** 1 PR

## Мета

Перейменувати LLMCall → ExternalServiceCall та створити unified service registry. Це підготовка для:
- Phase 3 (Snapshot simplification — FK на ExternalServiceCall)
- Non-LLM external service integrations (transcription APIs, тощо)

## Ключові зміни

1. **Rename table** `llm_calls` → `external_service_calls` (Alembic `op.rename_table()`)
2. **Rename/add fields**: `tokens_in` → `unit_in`, `tokens_out` → `unit_out`, `prompt_version` → `prompt_ref`, ADD `job_id` FK, ADD `unit_type`
3. **Config**: `config/models.yaml` → `config/external_services.yaml` з unified registry

## Критерії завершення

- [ ] ORM: ExternalServiceCall з 16 полями (spec в backlog.md)
- [ ] Repository: ExternalServiceCallRepository
- [ ] Config: `external_services.yaml` завантажується при старті
- [ ] Reports endpoint працює з новими іменами
- [ ] Worker стартує з новим config
