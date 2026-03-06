# S3-020: Recursive LLM Generation [SUPERSEDED]

**Тип:** New feature
**Пріоритет:** High
**Складність:** XL
**Phase:** 10
**Статус:** SUPERSEDED — розбита на S3-020a, S3-020b, S3-020c, S3-020d

## Опис

Multi-pass generation: bottom-up (leaf→root з children context) → top-down reconciliation (contradictions, gaps, terminology) → optional refinement після user edits.

Замінена на 4 підзадачі згідно ADR-recursive-generation.md:
- **S3-020a**: Контракти та рефакторинг Step Executor
- **S3-020b**: Bottom-up DAG оркестрація та per-node генерація
- **S3-020c**: Reconciliation pass (ковзне вікно)
- **S3-020d**: Selective refine pass
