# PD-006: Tenant-scoped Repositories

## Що

Модифікувати існуючі repositories для автоматичної фільтрації по `tenant_id`. Tenant A не бачить дані Tenant B.

## Навіщо

Data isolation — критична вимога для B2B API. Фільтрація на рівні repository гарантує що жоден endpoint не "забуде" перевірити tenant ownership.

## Ключові рішення

- `CourseRepository.__init__(session, tenant_id)` — tenant_id обов'язковий
- Всі SELECT додають `WHERE tenant_id = ?`
- Всі INSERT автоматично ставлять `tenant_id`
- `LLMCallRepository` + tenant_id для білінгу
- `create_log_callback()` отримує tenant_id для запису в llm_calls

## Acceptance Criteria

- [ ] `CourseRepository` фільтрує по tenant_id
- [ ] `LLMCallRepository` фільтрує по tenant_id
- [ ] CREATE operations автоматично ставлять tenant_id
- [ ] Тести підтверджують ізоляцію (tenant A не бачить курси tenant B)
