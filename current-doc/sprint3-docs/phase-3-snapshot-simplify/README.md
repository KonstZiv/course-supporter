# Phase 3: Snapshot Simplification

**Складність:** M (Medium)
**Залежності:** Phase 2 (ExternalServiceCall повинен існувати для FK)
**Задачі:** S3-010
**PR:** 1 PR

## Мета

Спростити StructureSnapshot — видалити дубльовані поля (prompt_version, model_id, tokens_in, tokens_out, cost_usd), додати FK на ExternalServiceCall як єдине джерело метаданих виклику.

## Ключові зміни

- Rename table: `course_structure_snapshots` → `structure_snapshots`
- DROP 5 полів (prompt_version, model_id, tokens_in, tokens_out, cost_usd)
- ADD `externalservicecall_id` FK
- `mode` та `node_fingerprint` залишаються

## Критерії завершення

- [ ] Snapshot має 6 полів (id, materialnode_id, externalservicecall_id, node_fingerprint, structure, created_at)
- [ ] Generation pipeline створює ExternalServiceCall + Snapshot пару
- [ ] Snapshot endpoints повертають LLM metadata через FK
