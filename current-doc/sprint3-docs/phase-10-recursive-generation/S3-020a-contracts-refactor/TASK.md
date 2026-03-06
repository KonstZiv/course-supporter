# S3-020a: Контракти та рефакторинг Step Executor

**Phase:** 10 (Recursive Generation)
**Складність:** L
**Статус:** PENDING
**Залежність:** S3-015 (StructureNode), S3-019 (Cascading failure)
**ADR:** `ADR-recursive-generation.md`

## Контекст

Перша задача фази 10. Визначає data contracts для всього pipeline, розширює існуючі моделі та рефакторить поточний arq_generate_structure у generic Step Executor. Backward compatible — поведінка системи не змінюється.

## Файли для зміни

| Файл | Дія | Зміни |
|------|-----|-------|
| `src/course_supporter/models/step.py` | NEW | StepType, StepInput, StepOutput, NodeSummary, Correction |
| `src/course_supporter/models/course.py` | EDIT | summary, core_concepts, mentioned_concepts + model_validator |
| `src/course_supporter/storage/orm.py` | EDIT | 5 нових колонок StructureSnapshot |
| `src/course_supporter/storage/snapshot_repository.py` | EDIT | Розширити create() новими полями |
| `src/course_supporter/api/tasks.py` | EDIT | arq_execute_step + backward-compatible wrapper |
| `src/course_supporter/agents/architect.py` | EDIT | execute(StepInput) -> StepOutput |
| `migrations/versions/xxxx_add_snapshot_step_fields.py` | NEW | ADD COLUMN x5 |
| `tests/unit/test_step_contracts.py` | NEW | Тести для StepInput/StepOutput/NodeSummary |
| `tests/unit/test_step_executor.py` | NEW | Тести для arq_execute_step |

## Деталі реалізації

### 1. Data contracts (models/step.py)

```python
class StepType(StrEnum):
    GENERATE = "generate"
    RECONCILE = "reconcile"
    REFINE = "refine"

@dataclass(frozen=True)
class NodeSummary:
    node_id: uuid.UUID
    title: str
    summary: str
    core_concepts: list[str]        # детально розкриті
    mentioned_concepts: list[str]   # згадані поверхнево
    structure_snapshot_id: uuid.UUID | None

@dataclass(frozen=True)
class StepInput:
    node_id: uuid.UUID
    step_type: StepType
    materials: list[SourceDocument]
    children_summaries: list[NodeSummary]
    parent_context: NodeSummary | None
    sibling_summaries: list[NodeSummary]
    existing_structure: str | None
    mode: Literal["free", "guided"]
    material_tree: list[MaterialNodeSummary]

@dataclass(frozen=True)
class StepOutput:
    structure: CourseStructure
    summary: str
    core_concepts: list[str]
    mentioned_concepts: list[str]
    prompt_version: str
    response: LLMResponse
    corrections: list[Correction] | None = None
    terminology_map: dict[str, str] | None = None
```

Інваріант: `set(core_concepts) & set(mentioned_concepts) == set()`.

### 2. Розширити CourseStructure

Додати до CourseStructure:
- `summary: str = ""`
- `core_concepts: list[str] = []`
- `mentioned_concepts: list[str] = []`
- `model_validator` що перевіряє непересічність множин

### 3. StructureSnapshot ORM + міграція

Нові nullable колонки:
- `summary: Mapped[str | None]` (Text)
- `core_concepts: Mapped[list[str] | None]` (JSONB)
- `mentioned_concepts: Mapped[list[str] | None]` (JSONB)
- `corrections: Mapped[dict[str, Any] | None]` (JSONB)
- `step_type: Mapped[str | None]` (String(20))

### 4. arq_execute_step

Generic Step Executor що замінює arq_generate_structure:
1. Job -> "active"
2. Завантажити вузол + матеріали
3. Побудувати StepInput (children/parent/sibling = пусті поки що)
4. Обрати Agent за step_type (тільки GenerateAgent)
5. Отримати StepOutput
6. Зберегти StructureSnapshot (з новими полями) + StructureNodes + ESC
7. Job -> "complete"

Старий arq_generate_structure залишається як thin wrapper.

### 5. ArchitectAgent.execute()

Новий метод що приймає StepInput, делегує існуючому run_with_metadata(), повертає StepOutput.

## Acceptance Criteria

- [ ] StepInput, StepOutput, NodeSummary, StepType визначені з типами
- [ ] CourseStructure має summary, core_concepts, mentioned_concepts з model_validator
- [ ] StructureSnapshot ORM має нові nullable колонки + міграція
- [ ] arq_execute_step працює для step_type="generate" ідентично старому arq_generate_structure
- [ ] ArchitectAgent.execute(StepInput) -> StepOutput працює
- [ ] Існуючі тести проходять без змін (backward compatible)
- [ ] Нові тести для контрактів та Step Executor
