# S1-021: ArchitectAgent Class (Step-Based Design)

## Мета

Реалізувати `ArchitectAgent` — async клас з **step-based архітектурою**, який приймає `CourseContext`, формує промпт, викликає LLM через `ModelRouter.complete_structured()` і повертає валідну `CourseStructure`. Кроки розділені на окремі методи для майбутньої міграції на LangGraph/DAG.

## Що робимо

1. **`PreparedPrompt`** (NamedTuple) — проміжний тип між кроками (system_prompt, user_prompt, prompt_version)
2. **`ArchitectAgent` клас** у `agents/architect.py`:
   - `__init__` з router + params
   - `async run(context) -> CourseStructure` — orchestrator
   - `_prepare_prompts(context) -> PreparedPrompt` — step 1 (sync)
   - `_generate(prepared) -> CourseStructure` — step 2 (async)
3. **Action**: `course_structuring` (вже в models.yaml)
4. **Logging**: structlog для start/finish з метриками
5. **~12 unit-тестів**: per-step + integration, mock router, verify params, propagate errors

## Архітектурне рішення

Замість монолітного `run()`, pipeline розбитий на методи-кроки:

```
run(context)
  ├─ _prepare_prompts(context) → PreparedPrompt    # step 1: sync
  └─ _generate(prepared)       → CourseStructure   # step 2: async
```

Кожен метод — потенційна нода графа. `PreparedPrompt` — частина проміжного State.

## Очікуваний результат

- `ArchitectAgent(router).run(context)` повертає `CourseStructure`
- `_prepare_prompts` тестується незалежно від LLM
- `_generate` тестується з PreparedPrompt напряму
- `AllModelsFailedError` пробрасывается
- `make check` проходить

## Контрольні точки

- [ ] `PreparedPrompt` — NamedTuple (system_prompt, user_prompt, prompt_version)
- [ ] `ArchitectAgent.__init__` приймає router + keyword params
- [ ] `_prepare_prompts(context) -> PreparedPrompt` — sync, load YAML + format
- [ ] `_generate(prepared) -> CourseStructure` — async, call router
- [ ] `run()` orchestrates: `_prepare_prompts → _generate`
- [ ] `action="course_structuring"`, `response_schema=CourseStructure`
- [ ] `AllModelsFailedError` propagated
- [ ] ~12 тестів зелені (4 per-step prepare + 3 per-step generate + 2 integration + 2 init)
- [ ] `make check` проходить

## Деталі

Повний spec: **T021-architect-agent.md**

## Блокує

- S1-022 (Persistence — зберігає результат ArchitectAgent)

## Блокується

- S1-019 (CourseStructure — response schema)
- S1-020 (Prompt loader — system/user prompt)
