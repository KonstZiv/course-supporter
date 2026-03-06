# S3-020c: Reconciliation pass (ковзне вікно)

**Тип:** New feature
**Пріоритет:** High
**Складність:** XL
**Phase:** 10

## Опис

Top-down прохід узгодження після bottom-up генерації. ReconcileAgent з ковзним вікном з 3 рівнів (батько <- поточний + sibling'и -> діти). Виявлення суперечностей, прогалин, нормалізація термінології.

## Вплив

- ReconcileAgent (новий)
- Промпти для reconciliation (нові)
- Orchestrator (top-down DAG)
- Step Executor (побудова ковзного вікна)
- Reconciliation apply logic (нова)
- Pydantic schema для reconciliation output (нова)

## Definition of Done

- ReconcileAgent з ковзним вікном працює
- Orchestrator створює top-down DAG для reconciliation
- Corrections зберігаються в structure_snapshots
- Тести для різних форм дерева
