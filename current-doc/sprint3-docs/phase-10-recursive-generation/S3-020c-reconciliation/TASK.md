# S3-020c: Reconciliation pass (ковзне вікно)

**Phase:** 10 (Recursive Generation)
**Складність:** XL
**Статус:** PENDING
**Залежність:** S3-020b
**ADR:** `ADR-recursive-generation.md`

## Контекст

Після bottom-up генерації (S3-020b) — top-down прохід узгодження. Для кожного вузла формується ковзне вікно з трьох рівнів: батько (макро), поточний + sibling'и, діти (мікро).

## Файли для зміни

| Файл | Дія | Зміни |
|------|-----|-------|
| `src/course_supporter/agents/reconcile.py` | NEW | ReconcileAgent |
| `src/course_supporter/models/reconciliation.py` | NEW | ReconciliationOutput schema |
| `src/course_supporter/reconciliation.py` | NEW | apply_corrections |
| `src/course_supporter/generation_orchestrator.py` | EDIT | top-down reconcile DAG |
| `src/course_supporter/api/tasks.py` | EDIT | sliding window + reconcile dispatch |
| `prompts/reconcile/v1.yaml` | NEW | Reconciliation промпт |
| `tests/unit/test_reconcile_agent.py` | NEW |
| `tests/unit/test_sliding_window.py` | NEW |
| `tests/unit/test_reconcile_orchestrator.py` | NEW |

## Деталі реалізації

### 1. ReconcileAgent

Приймає StepInput з step_type=RECONCILE. Використовує ковзне вікно:
- parent_context: summary батька (макро-контекст, якщо є)
- sibling_summaries: summaries сусідів (поточний рівень)
- children_summaries: summaries дітей (мікро-контекст, якщо є)

Повертає StepOutput з corrections та terminology_map.

### 2. Ковзне вікно в Step Executor

Для step_type="reconcile":
1. Завантажити summary батька -> parent_context
2. Завантажити summaries sibling'ів -> sibling_summaries
3. Завантажити summaries дітей -> children_summaries
4. Побудувати StepInput
5. Викликати ReconcileAgent.execute()

Рівні яких немає (root без батька, leaf без дітей) — ігноруються.

### 3. Top-down DAG в Orchestrator

Pre-order traversal (top-down):
- Root: Job(step_type="reconcile", depends_on=[all_generate_jobs])
- Вузол з дітьми: Job(step_type="reconcile", depends_on=[parent_reconcile_job])
- Leaf без дітей: reconciliation не потрібна

### 4. ReconciliationOutput (LLM response schema)

```python
class ReconciliationOutput(BaseModel):
    corrections: list[CorrectionItem]
    terminology_map: dict[str, str]
    gaps_detected: list[GapItem]
    contradictions_detected: list[ContradictionItem]
```

### 5. Apply corrections

Corrections зберігаються в snapshot для audit trail. Зміни застосовуються до structure_nodes.

## Acceptance Criteria

- [ ] ReconcileAgent приймає StepInput з ковзним вікном і повертає StepOutput
- [ ] Ковзне вікно правильно формується: батько + sibling'и + діти
- [ ] Рівні яких немає — ігноруються
- [ ] Orchestrator створює top-down DAG для reconciliation
- [ ] Corrections зберігаються в structure_snapshots.corrections
- [ ] Тести для ковзного вікна (різні форми дерева)
- [ ] Тести для ReconcileAgent (mock LLM)
