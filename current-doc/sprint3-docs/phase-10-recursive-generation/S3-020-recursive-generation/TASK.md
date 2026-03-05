# S3-020: Recursive LLM Generation Pipeline

**Phase:** 10 (Recursive Generation)
**Складність:** XL
**Статус:** PENDING
**Залежність:** S3-015 (StructureNode), S3-019 (Cascading failure)

## Контекст

Повна специфікація в `current-doc/backlog.md` → "Recursive LLM Generation Strategy + Reconciliation Passes".

## Архітектура

### Pass 1 — Bottom-up Generation

1. Leaf MaterialNodes → generate StructureNode tree independently
2. Parent nodes → generate with summaries of children as context
3. Continue up to root
4. Кожен node = окремий Job з `depends_on` на children jobs

```
Root MaterialNode
├── Topic A (depends_on: [subtopic A1, subtopic A2])
│   ├── Subtopic A1 (leaf, no deps)
│   └── Subtopic A2 (leaf, no deps)
└── Topic B (leaf, no deps)

Execution order: A1, A2, B (parallel) → A (after A1+A2) → Root (after A+B)
```

### Pass 2 — Top-down Reconciliation

After all nodes have initial structure:
1. **Contradiction detection** — conflicting definitions between siblings
2. **Gap detection** — missing prerequisites, broken concept dependencies
3. **Parent enrichment** — aggregate children data for parent descriptions
4. **Terminology normalization** — consistent naming across tree

### Pass 3 — Optional Refinement

After user edits StructureNode:
1. Re-run reconciliation only for affected subtree
2. Preserve user edits, suggest harmonization

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `src/course_supporter/generation_orchestrator.py` | MAJOR rewrite — per-node orchestration |
| `src/course_supporter/enqueue.py` | Per-node job enqueueing with depends_on chains |
| `src/course_supporter/api/tasks.py` | Per-node generation task |
| `src/course_supporter/agents/architect.py` | Context-aware prompts (children summaries) |
| `prompts/architect/` | Нові промпти для reconciliation |
| `src/course_supporter/api/routes/generation.py` | Multi-pass trigger API |
| `tests/` | Нові тести для per-node generation |

## Деталі реалізації

### 1. Per-node Job Creation

```python
async def trigger_recursive_generation(root_node_id, session):
    """Create generation jobs for entire tree, bottom-up."""
    tree = await node_repo.get_tree(root_node_id)
    jobs = {}

    def create_jobs_recursive(node):
        child_job_ids = []
        for child in node.children:
            child_job_id = create_jobs_recursive(child)
            child_job_ids.append(child_job_id)

        job = enqueue_generation(
            node_id=node.id,
            depends_on=child_job_ids,
            pass_type="generate",
        )
        jobs[node.id] = job.id
        return job.id

    create_jobs_recursive(tree)
    return jobs
```

### 2. Context-aware Generation

```python
async def arq_generate_node(ctx, job_id, node_id, pass_type):
    # Collect children summaries (already generated)
    children_summaries = await get_children_summaries(node_id)

    # Build prompt with children context
    context = build_generation_context(
        node=node,
        materials=materials,
        children_summaries=children_summaries,
    )

    result = await architect.run_with_metadata(context, ...)
    # Convert to StructureNode tree
    await persist_structure_nodes(result, snapshot_id, node_id)
```

### 3. Reconciliation Pass

```python
async def trigger_reconciliation(root_node_id, session):
    """Pass 2: top-down reconciliation."""
    # For each parent with children:
    # 1. Collect all children StructureNodes
    # 2. Run contradiction/gap detection prompt
    # 3. Apply corrections
    # 4. Normalize terminology
```

## Open Questions (to resolve during implementation)

1. Reconciliation prompt design — per-parent with all children, or pairwise?
2. How to handle conflicting suggestions?
3. Cost optimization — fingerprint-based caching?
4. Snapshot granularity — per node per pass, or per tree per pass?

## Acceptance Criteria

- [ ] Pass 1: bottom-up generation з correct depends_on chains
- [ ] Children summaries passed as context to parent generation
- [ ] Pass 2: reconciliation detects contradictions and gaps
- [ ] Pass 3: selective re-generation for edited subtrees
- [ ] Jobs cascade failure correctly (Phase 9 dependency)
- [ ] Cost tracking via ExternalServiceCall per node
