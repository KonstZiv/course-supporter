# S3-020b: Bottom-up DAG оркестрація та per-node генерація

**Тип:** New feature
**Пріоритет:** High
**Складність:** L
**Phase:** 10

## Опис

Orchestrator створює DAG задач для bottom-up генерації (Pass 1). Кожен вузол генерується окремо з children summaries як контекст. Post-order traversal: leaf'и паралельно, parent'и чекають на дітей.

## Вплив

- generation_orchestrator (trigger_recursive_generation)
- enqueue (enqueue_step)
- Step Executor (children_summaries loading)
- ArchitectAgent (children context в промпті)
- API endpoint (новий orchestrator call)
- Промпти (children_context placeholder)

## Definition of Done

- Per-node jobs з правильними depends_on (bottom-up)
- Children summaries передаються як контекст LLM
- GenerationPlan містить список jobs + estimated_llm_calls
- Cascading failure працює через DAG
