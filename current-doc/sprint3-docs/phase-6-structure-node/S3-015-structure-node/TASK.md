# S3-015: StructureNode — Recursive Output Structure

**Phase:** 6 (StructureNode)
**Складність:** XL
**Статус:** PENDING
**Залежність:** S3-014 (SourceMaterial removed)

## Контекст

Поточна output structure — rigid 4-level hierarchy: Module → Lesson → Concept → Exercise (4 таблиці). Це не може mirror arbitrary-depth input trees. Замінюємо на рекурсивний StructureNode (adjacency list, як MaterialNode).

Повна специфікація полів: `current-doc/backlog.md`.

## Нова таблиця `structure_nodes`

### System fields
- `id` UUID PK
- `structuresnapshot_id` UUID FK → structure_snapshots
- `parent_structurenode_id` UUID FK → self (NULL = root)
- `node_type` StrEnum: module, lesson, concept, exercise (extensible)
- `order` int
- `created_at`, `updated_at` DateTime(tz)

### Section 1 — Formal (Methodologist agent)
- `title` String NOT NULL
- `description` Text nullable
- `learning_goal` Text nullable
- `expected_knowledge` JSONB nullable — list[{summary, details}]
- `expected_skills` JSONB nullable — list[{summary, details}]
- `prerequisites` JSONB nullable — list[str]
- `difficulty` String nullable — easy|medium|hard
- `estimated_duration` int nullable — minutes

### Section 2 — Results (Methodologist agent)
- `success_criteria` Text nullable
- `assessment_method` String nullable
- `competencies` JSONB nullable — list[str]

### Section 3 — Methodological (Methodologist agent)
- `key_concepts` JSONB nullable — list[{summary, details}]
- `common_mistakes` JSONB nullable — list[str]
- `teaching_strategy` String nullable
- `activities` JSONB nullable — list[str]

### Section 4 — Context (Methodologist agent)
- `teaching_style` String nullable
- `deep_dive_references` JSONB nullable
- `content_version` DateTime nullable

### Section 5 — Material refs (Indexer agent, AFTER methodologist)
- `timecodes` JSONB nullable
- `slide_references` JSONB nullable
- `web_references` JSONB nullable

### Section 6 — Semantic (Embedding pipeline)
- `embedding` Vector(1536)

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `src/course_supporter/storage/orm.py` | ВИДАЛИТИ Module, Lesson, Concept, Exercise. ДОДАТИ StructureNode |
| `src/course_supporter/storage/course_structure_repository.py` | ВИДАЛИТИ → замінити StructureNodeRepository |
| `src/course_supporter/storage/lesson_repository.py` | ВИДАЛИТИ |
| `src/course_supporter/api/tasks.py` | Rewrite persistence в `arq_generate_structure` — LLM output → StructureNode tree |
| `src/course_supporter/api/schemas.py` | ВИДАЛИТИ Module/Lesson/Concept/ExerciseResponse. ДОДАТИ StructureNodeResponse (recursive) |
| `src/course_supporter/api/routes/generation.py` | Update response format |
| `src/course_supporter/api/routes/` | Видалити lesson endpoint |
| `src/course_supporter/models/course.py` | Pydantic models залишаються для LLM output parsing |
| `migrations/versions/` | CREATE TABLE + data migration + DROP 4 tables |
| `tests/` | Видалити `test_lesson_detail.py`, `test_course_structure_repository.py`; створити нові |

## Деталі реалізації

### 1. StructureNodeRepository

```python
class StructureNodeRepository:
    async def create_tree(self, snapshot_id: uuid.UUID, nodes: list[StructureNodeCreate]) -> list[StructureNode]:
        """Persist entire tree from LLM output."""
        ...

    async def get_tree(self, snapshot_id: uuid.UUID) -> list[StructureNode]:
        """Load full tree for snapshot."""
        ...

    async def get_by_type(self, snapshot_id: uuid.UUID, node_type: str) -> list[StructureNode]:
        """Filter nodes by type."""
        ...
```

### 2. LLM Output → StructureNode Conversion (tasks.py)

```python
# LLM output (unchanged):
# CourseStructure → Module → Lesson → Concept/Exercise

# Conversion:
def _convert_to_structure_nodes(structure: CourseStructure, snapshot_id: uuid.UUID) -> list[StructureNode]:
    nodes = []
    for i, module in enumerate(structure.modules):
        mod_node = StructureNode(
            structuresnapshot_id=snapshot_id,
            parent_structurenode_id=None,  # root of generated structure
            node_type="module",
            order=i,
            title=module.title,
            description=module.description,
            learning_goal=module.learning_goal,
            # ... map other fields
        )
        nodes.append(mod_node)
        for j, lesson in enumerate(module.lessons):
            les_node = StructureNode(
                parent_structurenode_id=mod_node.id,
                node_type="lesson",
                order=j,
                # ...
            )
            nodes.append(les_node)
            # ... concepts, exercises
    return nodes
```

### 3. Recursive Pydantic Response

```python
class StructureNodeResponse(BaseModel):
    id: uuid.UUID
    node_type: str
    order: int
    title: str
    description: str | None = None
    children: list[StructureNodeResponse] = []
    # ... інші поля

    model_config = ConfigDict(from_attributes=True)
```

### 4. Data Migration

```python
# For each existing Module → create StructureNode(node_type="module")
# For each Lesson → create StructureNode(node_type="lesson", parent=module_node)
# For each Concept → create StructureNode(node_type="concept", parent=lesson_node)
# For each Exercise → create StructureNode(node_type="exercise", parent=lesson_node)
```

### 5. Drop Old Tables

```python
op.drop_table("exercises")
op.drop_table("concepts")
op.drop_table("lessons")
op.drop_table("modules")
```

## Two-level JSONB Structure

`key_concepts`, `expected_knowledge`, `expected_skills` use identical format:
```json
[{"summary": "short label", "details": "expanded for indexing"}]
```

## Acceptance Criteria

- [ ] StructureNode table з всіма 25+ полями
- [ ] Module/Lesson/Concept/Exercise tables видалені
- [ ] StructureNodeRepository з create_tree, get_tree
- [ ] LLM output → StructureNode conversion в task
- [ ] Recursive StructureNodeResponse в API
- [ ] Data migration від 4 таблиць
- [ ] Тести покривають CRUD, conversion, recursive response
