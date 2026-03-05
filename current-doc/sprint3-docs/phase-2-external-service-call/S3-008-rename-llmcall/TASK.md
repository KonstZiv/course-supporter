# S3-008: Rename LLMCall → ExternalServiceCall

**Phase:** 2 (ExternalServiceCall + Config)
**Складність:** M
**Статус:** PENDING

## Контекст

`LLMCall` table відстежує тільки LLM API calls. Система розширюється для non-LLM сервісів (transcription APIs, тощо) з різними pricing models. Потрібен універсальний журнал всіх зовнішніх сервісів.

## Повна специфікація полів (16 fields)

Зафіксована в `current-doc/backlog.md`:

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID PK | — |
| `tenant_id` | UUID FK → Tenant | nullable (NULL = system calls) |
| `job_id` | UUID FK → Job | **NEW**, nullable (NULL = outside job queue) |
| `action` | String(100) | transcribe, describe_slides, generate_structure |
| `strategy` | String(50) | model selection strategy |
| `provider` | String(50) | gemini, anthropic, openai, ... |
| `model_id` | String(100) | specific model/service |
| `prompt_ref` | String(50) | **RENAMED** from prompt_version |
| `unit_type` | String(20) | **NEW**, tokens/minutes/chars |
| `unit_in` | int | **RENAMED** from tokens_in |
| `unit_out` | int | **RENAMED** from tokens_out |
| `latency_ms` | int | — |
| `cost_usd` | float | — |
| `success` | bool | default True |
| `error_message` | Text | — |
| `created_at` | DateTime(tz) | — |

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `src/course_supporter/storage/orm.py` | Rename class LLMCall → ExternalServiceCall, update fields |
| `src/course_supporter/storage/llm_call_repository.py` | Rename → `external_service_call_repository.py` |
| `src/course_supporter/llm/logging.py` | Update imports, field names |
| `src/course_supporter/api/routes/reports.py` | Update repository usage |
| `src/course_supporter/api/schemas.py` | Rename LLMCallResponse → ExternalServiceCallResponse |
| `migrations/versions/` | `op.rename_table()` + `op.alter_column()` + ADD columns |
| `tests/` | Update all references |

## Migration

```python
def upgrade():
    # 1. Rename table
    op.rename_table("llm_calls", "external_service_calls")

    # 2. Rename columns
    op.alter_column("external_service_calls", "tokens_in", new_column_name="unit_in")
    op.alter_column("external_service_calls", "tokens_out", new_column_name="unit_out")
    op.alter_column("external_service_calls", "prompt_version", new_column_name="prompt_ref")

    # 3. Add new columns
    op.add_column("external_service_calls",
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id"), nullable=True))
    op.add_column("external_service_calls",
        sa.Column("unit_type", sa.String(20), nullable=True))
```

## Acceptance Criteria

- [ ] Table renamed to `external_service_calls`
- [ ] All 16 fields present with correct types
- [ ] Repository renamed and updated
- [ ] All references updated (imports, routes, schemas, tests)
- [ ] Migration проходить (upgrade + downgrade)
- [ ] Reports endpoint працює
