# S3-020b: Bottom-up DAG оркестрація та per-node генерація

**Phase:** 10 (Recursive Generation)
**Складність:** L
**Статус:** PENDING
**Залежність:** S3-020a
**ADR:** `ADR-recursive-generation.md`

## Контекст

Після визначення контрактів (S3-020a) — реалізуємо per-node генерацію. Orchestrator створює DAG задач bottom-up: leaf'и паралельно, parent'и чекають на завершення дітей. Step Executor заповнює children_summaries з БД.

## Файли для зміни

| Файл | Дія | Зміни |
|------|-----|-------|
| `src/course_supporter/generation_orchestrator.py` | EDIT | trigger_recursive_generation, GenerationPlan розширення |
| `src/course_supporter/enqueue.py` | EDIT | enqueue_step (generic) |
| `src/course_supporter/api/tasks.py` | EDIT | children_summaries loading в arq_execute_step |
| `src/course_supporter/agents/architect.py` | EDIT | children context в промпті |
| `src/course_supporter/api/routes/generation.py` | EDIT | новий orchestrator call |
| `prompts/architect/v1.yaml` | EDIT | children_context placeholder |
| `prompts/architect/v1_guided.yaml` | EDIT | children_context placeholder |
| `tests/unit/test_recursive_orchestrator.py` | NEW | DAG creation тести |
| `tests/unit/test_step_executor_children.py` | NEW | children_summaries тести |

## Деталі реалізації

### 1. trigger_recursive_generation

Post-order traversal (bottom-up):
- Leaf: Job(step_type="generate", depends_on=[ingestion_jobs])
- Parent: Job(step_type="generate", depends_on=[children_generate_jobs])

### 2. enqueue_step

Замінює enqueue_generation для нових step types. Enqueue arq_execute_step.

### 3. Children summaries в Step Executor

Для step_type="generate":
1. Знайти дочірні вузли поточного node
2. Для кожного: прочитати найсвіжіший StructureSnapshot (summary, core_concepts, mentioned_concepts)
3. Побудувати list[NodeSummary]
4. Передати в StepInput.children_summaries

### 4. Промпти з children context

Додати placeholder {children_context} та інструкції:
"Ці підтеми вже згенеровані. Використай їх підсумки для побудови загальної структури."

### 5. GenerationPlan розширення

```python
@dataclass(frozen=True, slots=True)
class GenerationPlan:
    ingestion_jobs: list[Job]
    generation_jobs: list[Job]          # WAS: generation_job (singular)
    reconciliation_jobs: list[Job]      # NEW (порожній поки що)
    existing_snapshot_id: uuid.UUID | None
    is_idempotent: bool
    mapping_warnings: list[MappingWarning]
    estimated_llm_calls: int            # NEW
```

## Acceptance Criteria

- [ ] Orchestrator створює per-node jobs з правильними depends_on (bottom-up)
- [ ] Leaf вузли генеруються без залежностей (або після ingestion)
- [ ] Parent вузли чекають на завершення children jobs
- [ ] arq_execute_step заповнює children_summaries з БД
- [ ] ArchitectAgent використовує children_summaries в промпті
- [ ] GenerationPlan містить список jobs + estimated_llm_calls
- [ ] Cascading failure працює через DAG
- [ ] Інтеграційний тест: дерево з 3 рівнями -> правильний порядок jobs
