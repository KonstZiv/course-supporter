# S3-020d: Selective refine pass

**Phase:** 10 (Recursive Generation)
**Складність:** M
**Статус:** PENDING
**Залежність:** S3-020c
**ADR:** `ADR-recursive-generation.md`

## Контекст

Після правок користувача в structure_nodes — перегенерація лише зміненого піддерева зі збереженням ручних правок. Ковзне вікно використовується для контексту.

## Файли для зміни

| Файл | Дія | Зміни |
|------|-----|-------|
| `src/course_supporter/agents/refine.py` | NEW | RefineAgent |
| `src/course_supporter/generation_orchestrator.py` | EDIT | trigger_refine |
| `src/course_supporter/api/routes/generation.py` | EDIT | POST /nodes/{node_id}/refine |
| `src/course_supporter/api/tasks.py` | EDIT | refine dispatch |
| `prompts/refine/v1.yaml` | NEW | Refine промпт |
| `tests/unit/test_refine_agent.py` | NEW |
| `tests/unit/test_refine_orchestrator.py` | NEW |

## Деталі реалізації

### 1. RefineAgent

Приймає StepInput з step_type=REFINE:
- existing_structure: JSON поточних StructureNodes (з правками користувача)
- Ковзне вікно: батько + sibling'и + діти

Промпт: "Структура була відредагована. Збережи ручні правки. Гармонізуй зі сусідами."

### 2. trigger_refine

1. Визначити змінений вузол
2. Перегенерувати лише цей вузол (refine)
3. Reconcile предків вгору до root
4. Sibling'и не перегенеровуються — лише контекст

### 3. API endpoint

POST /nodes/{node_id}/refine — запускає selective refine після ручних правок.

## Acceptance Criteria

- [ ] RefineAgent зберігає ручні правки користувача
- [ ] Перегенерація лише зміненого вузла + предків
- [ ] Sibling'и використовуються як контекст, не перегенеровуються
- [ ] Ковзне вікно працює для refine
- [ ] API endpoint POST /nodes/{node_id}/refine
- [ ] Тести для RefineAgent (mock LLM)
- [ ] Тести для trigger_refine (правильний scope)
