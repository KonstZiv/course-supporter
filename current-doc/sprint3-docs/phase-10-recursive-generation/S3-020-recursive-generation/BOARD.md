# S3-020: Recursive LLM Generation

**Тип:** New feature
**Пріоритет:** High
**Складність:** XL
**Phase:** 10

## Опис

Multi-pass generation: bottom-up (leaf→root з children context) → top-down reconciliation (contradictions, gaps, terminology) → optional refinement після user edits.

## Вплив

- Generation orchestrator (major rewrite)
- Enqueue, tasks, prompts
- Per-node Job chains з depends_on

## Definition of Done

- 3-pass pipeline працює
- Bottom-up з children context
- Reconciliation detects issues
