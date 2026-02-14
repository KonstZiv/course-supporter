# S1-021: ArchitectAgent Class

## Мета

Реалізувати `ArchitectAgent` — async клас, який приймає `CourseContext`, формує промпт, викликає LLM через `ModelRouter.complete_structured()` і повертає валідну `CourseStructure`.

## Що робимо

1. **`ArchitectAgent` клас** у `agents/architect.py` — `__init__` з router + params, `async run(context) -> CourseStructure`
2. **Orchestration**: load prompt → serialize context → call router → return parsed result
3. **Action**: `course_structuring` (вже в models.yaml)
4. **Logging**: structlog для start/finish з метриками
5. **~10 unit-тестів**: mock router, verify params, propagate errors

## Очікуваний результат

- `ArchitectAgent(router).run(context)` повертає `CourseStructure`
- Використовує `router.complete_structured()` з `action="course_structuring"`
- System prompt із YAML, user prompt з serialized context
- `AllModelsFailedError` пробрасывается
- `make check` проходить

## Контрольні точки

- [ ] `ArchitectAgent.__init__` приймає router + keyword params
- [ ] `run()` серіалізує context → format prompt → call router
- [ ] `action="course_structuring"`, `response_schema=CourseStructure`
- [ ] Повертає parsed `CourseStructure` (перший елемент tuple)
- [ ] `AllModelsFailedError` propagated
- [ ] ~10 тестів зелені
- [ ] `make check` проходить

## Деталі

Повний spec: **T021-architect-agent.md**

## Блокує

- S1-022 (Persistence — зберігає результат ArchitectAgent)

## Блокується

- S1-019 (CourseStructure — response schema)
- S1-020 (Prompt loader — system/user prompt)
