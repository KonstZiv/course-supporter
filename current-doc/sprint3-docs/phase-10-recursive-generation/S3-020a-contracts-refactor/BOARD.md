# S3-020a: Контракти та рефакторинг Step Executor

**Тип:** Refactoring
**Пріоритет:** High
**Складність:** L
**Phase:** 10

## Опис

Визначити data contracts (StepInput, StepOutput, NodeSummary), розширити CourseStructure (summary, core/mentioned concepts), додати нові колонки до StructureSnapshot, рефакторити arq_generate_structure у generic Step Executor. Backward compatible — поведінка системи не змінюється.

## Вплив

- Нові data contracts (models/step.py)
- CourseStructure розширення + model_validator
- StructureSnapshot ORM + міграція (5 нових nullable колонок)
- arq_execute_step (generic Step Executor)
- ArchitectAgent.execute() (StepInput/StepOutput wrapper)

## Definition of Done

- StepInput, StepOutput, NodeSummary, StepType визначені
- CourseStructure має summary, core_concepts, mentioned_concepts з інваріантом
- StructureSnapshot ORM має нові колонки + міграція
- arq_execute_step працює для step_type="generate"
- Існуючі тести проходять без змін
