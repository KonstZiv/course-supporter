# S1-020: System Prompt v1 + Prompt Loader

## Мета

Створити system prompt для ArchitectAgent у `prompts/architect/v1.yaml` та утиліти для завантаження/форматування промптів.

## Що робимо

1. **System prompt** у YAML — role description, output format rules, quality criteria
2. **User prompt template** з `{context}` placeholder для CourseContext
3. **`prompt_loader.py`** — `load_prompt(path)`, `format_user_prompt(template, context)`
4. **~8 unit-тестів**: load valid/missing, format template, actual v1.yaml loads

## Очікуваний результат

- `prompts/architect/v1.yaml` містить повний промпт
- `load_prompt()` повертає dict з `system_prompt` та `user_prompt_template`
- `format_user_prompt()` підставляє context у шаблон
- Error handling: FileNotFoundError, KeyError
- `make check` проходить

## Контрольні точки

- [ ] `v1.yaml` містить system_prompt та user_prompt_template
- [ ] `load_prompt()` працює з валідним файлом
- [ ] `load_prompt()` raises на missing file / missing keys
- [ ] `format_user_prompt()` інжектить context
- [ ] ~8 тестів зелені
- [ ] `make check` проходить

## Деталі

Повний spec: **T020-architect-prompt.md**

## Блокує

- S1-021 (ArchitectAgent використовує prompt)

## Блокується

- S1-019 (CourseStructure schema описана в промпті)
