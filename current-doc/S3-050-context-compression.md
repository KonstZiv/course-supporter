# S3-050: Context Compression for Parent/Root Node Generation

## Problem

When generating structure for a parent/root node, `_collect_ready_documents()` collects **all raw materials from the entire subtree**. For a course with 3 child nodes this produces ~445k input tokens — exceeding Gemini free tier rate limit (250k/min) and making generation expensive on paid tiers.

The root cause: parent nodes receive full transcripts, slide text, and web content from all descendants, even though those descendants **already have generated snapshots** with structured summaries.

## Solution

Replace raw descendant materials with **child snapshots** for non-leaf nodes. Add a new field `summary_nested_nodes` to enable recursive context compression with a gradient of detail:

| Distance from node | What LLM sees | Volume |
|---|---|---|
| 0 (current node) | Raw materials (processed_content) | 100% |
| -1 (direct children) | Full snapshots (structure + summary + concepts + summary_nested_nodes) | ~5-10% |
| -2+ (grandchildren) | Only via `summary_nested_nodes` in children's snapshots | ~0.5-1% |

## Acceptance Criteria

- [ ] Parent/root nodes use child snapshots instead of child raw materials
- [ ] Leaf nodes continue using raw materials (no change)
- [ ] New field `summary_nested_nodes` in `StructureSnapshot` ORM and `CourseStructure` schema
- [ ] LLM prompt instructs generation of `summary_nested_nodes` from received child snapshots
- [ ] Root node with 3 children generates successfully within Gemini free tier limits
- [ ] All existing tests pass; new tests cover the changed context assembly

## Architecture

### Current Flow (parent node)

```
arq_execute_step()
  → get_subtree(root_id, include_materials=True)      # loads ALL materials
  → _collect_ready_documents(flat_nodes)               # ALL descendants' raw content
  → MergeStep().merge(documents, ...)                  # serialize ALL to CourseContext
  → ArchitectAgent.execute(step_input)                 # ~445k tokens to LLM
```

### New Flow (parent node)

```
arq_execute_step()
  → get_subtree(root_id, include_materials=True)
  → _collect_ready_documents([target_node_only])       # ONLY this node's materials
  → _load_children_snapshots(session, target_node)     # children's full snapshots
  → MergeStep().merge(own_documents, ...)              # own materials only
  → ArchitectAgent.execute(step_input)                 # ~30-50k tokens to LLM
```

### Data in Prompt (parent node)

```
## This Node's Materials          ← raw processed_content (if any)
{context}

## Child Node Snapshots           ← NEW: full CourseStructure from children
### Node "01 - Умовні конструкції"
Structure: {modules: [...], summary: "...", core_concepts: [...]}
Summary of nested nodes: "..."   ← from summary_nested_nodes field

### Node "02 - Цикли"
Structure: {modules: [...], summary: "...", core_concepts: [...]}
Summary of nested nodes: "..."
```

## Implementation Steps

### Step 1: Add `summary_nested_nodes` field

**Files:**
- `src/course_supporter/storage/orm.py` — add column to `StructureSnapshot`
- `src/course_supporter/models/course.py` — add field to `CourseStructure`
- Alembic migration

```python
# orm.py — StructureSnapshot
summary_nested_nodes: Mapped[str | None] = mapped_column(
    Text,
    nullable=True,
    comment="LLM-generated compressed summary of all nested node snapshots",
)

# course.py — CourseStructure
summary_nested_nodes: str = ""
```

### Step 2: Update prompt to generate `summary_nested_nodes`

**Files:**
- `prompts/architect/v1.yaml` — add field description + generation instruction
- `prompts/architect/v1_guided.yaml` — same

Add to Output Schema section:
```
### Course level (additional fields)
- **summary_nested_nodes**: compressed summary of ALL child node snapshots
  received in the "Child Node Snapshots" section. Should capture key topics,
  learning goals, and concept coverage across all nested materials.
  Empty string if no child snapshots were provided.
```

Add to Rules:
```
10. If "Child Node Snapshots" are provided, generate summary_nested_nodes
    as a concise (200-500 word) summary that captures the key topics,
    learning goals, and concepts from ALL nested node structures.
    This summary will be used by parent nodes as compressed context.
```

### Step 3: Add `children_snapshots` to `StepInput`

**Files:**
- `src/course_supporter/models/step.py` — add field to `StepInput`

```python
@dataclass(frozen=True)
class ChildSnapshotContext:
    """Full snapshot of a child node for parent context."""
    node_id: uuid.UUID
    title: str
    structure: dict[str, Any]       # CourseStructure JSON
    summary: str
    core_concepts: list[str]
    mentioned_concepts: list[str]
    summary_nested_nodes: str       # compressed grandchildren context

@dataclass(frozen=True)
class StepInput:
    ...
    # NEW: full child snapshots for parent nodes
    children_snapshots: list[ChildSnapshotContext] = field(default_factory=list)
```

### Step 4: Change context assembly in `tasks.py`

**Files:**
- `src/course_supporter/api/tasks.py`

Key change in `arq_execute_step()`:

```python
# BEFORE: collect documents from entire subtree
documents = _collect_ready_documents(flat_nodes)

# AFTER: collect documents from target node only (if it has children with snapshots)
if target_node.children and children_snapshots:
    # Parent node: own materials only
    own_documents = _collect_ready_documents([target_node])
else:
    # Leaf node: all materials (unchanged behavior)
    own_documents = _collect_ready_documents(flat_nodes)
```

New function to load full child snapshots:

```python
async def _load_children_snapshots(
    session: AsyncSession,
    node: MaterialNode,
) -> list[ChildSnapshotContext]:
    """Load full snapshot data from child nodes for parent context."""
    ...
```

### Step 5: Update `ArchitectAgent` to format child snapshots

**Files:**
- `src/course_supporter/agents/architect.py`

Replace `_format_children_context()` (which only shows summary + concepts) with `_format_children_snapshots()` that includes the full CourseStructure JSON:

```python
def _format_children_snapshots(
    children_snapshots: list[ChildSnapshotContext],
) -> str:
    """Format child snapshots as structured context for the LLM prompt."""
    if not children_snapshots:
        return ""
    lines = ["## Child Node Snapshots", ""]
    for cs in children_snapshots:
        lines.append(f"### {cs.title}")
        lines.append(f"**Summary:** {cs.summary}")
        lines.append(f"**Core concepts:** {', '.join(cs.core_concepts)}")
        if cs.summary_nested_nodes:
            lines.append(f"**Nested nodes summary:** {cs.summary_nested_nodes}")
        lines.append(f"**Structure:**")
        lines.append(json.dumps(cs.structure, ensure_ascii=False, indent=2))
        lines.append("")
    return "\n".join(lines)
```

### Step 6: Persist `summary_nested_nodes` in snapshot

**Files:**
- `src/course_supporter/api/tasks.py` — `_persist_step_result()`
- `src/course_supporter/storage/snapshot_repository.py` — `create()`

```python
# _persist_step_result
snapshot = await snap_repo.create(
    ...
    summary_nested_nodes=step_output.summary_nested_nodes,  # NEW
)
```

### Step 7: Update `StepOutput`

**Files:**
- `src/course_supporter/models/step.py`

```python
@dataclass(frozen=True)
class StepOutput:
    ...
    summary_nested_nodes: str = ""  # NEW
```

## Token Budget Estimate (3-node course)

| Scenario | Before | After |
|---|---|---|
| Leaf node (own materials) | ~130-194k | ~130-194k (unchanged) |
| Root node (3 children) | ~445k | ~30-50k |
| Total pipeline | ~770k | ~400-440k |
| Root fits in Gemini free tier? | No (>250k) | Yes (<250k) |

## Backwards Compatibility

- `summary_nested_nodes` is nullable/optional — existing snapshots unaffected
- Leaf nodes produce `summary_nested_nodes=""` — no impact
- `_collect_ready_documents` for leaf nodes unchanged
- Old `arq_generate_structure()` task is untouched (only `arq_execute_step` modified)

## Edge Cases

1. **Node has own materials AND children** — include both own raw materials and children snapshots
2. **Child has no snapshot yet** — fall back to collecting that child's raw materials (partial compression)
3. **Empty `summary_nested_nodes`** — leaf nodes always produce empty string; parent prompt handles gracefully
4. **Guided mode** — same compression applies; existing_structure still comes from serialize_tree_for_guided
