# Phase 10: Recursive LLM Generation

**Складність:** XL (Extra Large)
**Залежності:** Phase 6 (StructureNode) + Phase 9 (Cascading failure)
**Задачі:** S3-020
**PRs:** 2-3 PRs (pass by pass)
**Risk:** MEDIUM

## Мета

Multi-pass LLM generation strategy:
1. **Pass 1 (Bottom-up):** leaf nodes → parent nodes → root
2. **Pass 2 (Top-down reconciliation):** contradictions, gaps, terminology
3. **Pass 3 (Optional):** refinement after user edits

## Контекст

Per-node generation дає кращі результати ніж whole-course. Але незалежно оброблені nodes можуть мати протиріччя, overlap, inconsistent terminology.

## Критерії завершення

- [ ] Pass 1: bottom-up generation з children context
- [ ] Pass 2: reconciliation (contradictions, gaps, terminology)
- [ ] Pass 3: optional refinement для edited subtrees
- [ ] Кожен pass як окремий Job з depends_on
