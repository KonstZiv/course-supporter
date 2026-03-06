# Фаза 10: Рекурсивний pipeline генерації

**ADR:** `ADR-recursive-generation.md` (затверджений)
**Залежності:** S3-015 (StructureNode), S3-019 (Cascading failure) — обидві DONE

---

## Огляд

Фаза розбита на 4 задачі (S3-020a..d), кожна — окремий PR.
Кожна задача залишає систему у робочому стані — поточна функціональність не ламається.

```
S3-020a  Контракти + рефакторинг поточного pipeline
    ↓
S3-020b  Bottom-up DAG оркестрація + per-node генерація
    ↓
S3-020c  Reconciliation pass (ковзне вікно)
    ↓
S3-020d  Selective refine pass (після правок користувача)
```

---

## S3-020a: Контракти та рефакторинг Step Executor

**Складність:** L
**Мета:** Визначити data contracts (`StepInput`, `StepOutput`, `NodeSummary`), розширити `CourseStructure` та `StructureSnapshot`, рефакторити `arq_generate_structure` у generic Step Executor. Поведінка системи не змінюється — це internal refactoring.

### Що робимо

#### 1. Нові data contracts

**Файл:** `src/course_supporter/models/step.py` (NEW)

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
    core_concepts: list[str]
    mentioned_concepts: list[str]
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

#### 2. Розширити CourseStructure (LLM response schema)

**Файл:** `src/course_supporter/models/course.py`

Додати до `CourseStructure`:
```python
summary: str = ""                       # стиснутий підсумок згенерованої структури
core_concepts: list[str] = []           # концепти, детально розкриті
mentioned_concepts: list[str] = []      # концепти, згадані поверхнево
```

**Інваріант:** `set(core_concepts) & set(mentioned_concepts) == set()` — валідація в `model_validator`.

#### 3. Розширити StructureSnapshot (ORM + міграція)

**Файл:** `src/course_supporter/storage/orm.py`

Додати колонки до `StructureSnapshot`:
```python
summary: Mapped[str | None]                    # Text, nullable
core_concepts: Mapped[list[str] | None]        # JSONB, nullable
mentioned_concepts: Mapped[list[str] | None]   # JSONB, nullable
corrections: Mapped[dict[str, Any] | None]     # JSONB, nullable
step_type: Mapped[str | None]                  # String(20), nullable ("generate"/"reconcile"/"refine")
```

**Файл:** `migrations/versions/xxxx_add_snapshot_step_fields.py` (NEW)
- ADD COLUMN `summary`, `core_concepts`, `mentioned_concepts`, `corrections`, `step_type`
- Усі nullable — backward compatible з існуючими записами

#### 4. Рефакторинг arq_generate_structure → Step Executor

**Файл:** `src/course_supporter/api/tasks.py`

Розбити поточну `arq_generate_structure()` на:

```python
async def arq_execute_step(
    ctx: dict[str, Any],
    job_id: str,
    node_id: str,
    step_type: str,              # "generate" | "reconcile" | "refine"
    mode: str = "free",
) -> None:
    """Generic Step Executor: load data → call Agent → persist result."""
```

Алгоритм:
1. Job → "active"
2. Завантажити вузол + матеріали
3. Побудувати `StepInput` (поки що: children/parent/sibling summaries = пусті)
4. Вибрати Agent за `step_type` (поки що: тільки `GenerateAgent`)
5. Отримати `StepOutput`
6. Зберегти `StructureSnapshot` (з новими полями summary, concepts, step_type)
7. Зберегти `StructureNodes`
8. Job → "complete"

**Старий `arq_generate_structure` залишається як thin wrapper** що викликає `arq_execute_step` з `step_type="generate"` — для backward compatibility з вже enqueueded jobs.

#### 5. Обгорнути ArchitectAgent у StepInput/StepOutput контракт

**Файл:** `src/course_supporter/agents/architect.py`

Додати метод:
```python
async def execute(self, step_input: StepInput) -> StepOutput:
    """Execute generation step from StepInput contract."""
    # Build CourseContext from step_input.materials + step_input.material_tree
    # Call run_with_metadata()
    # Extract summary, core_concepts, mentioned_concepts from CourseStructure
    # Return StepOutput
```

Існуючі `run()` та `run_with_metadata()` залишаються — `execute()` делегує їм.

#### 6. Оновити SnapshotRepository

**Файл:** `src/course_supporter/storage/snapshot_repository.py`

Розширити `create()`:
```python
async def create(
    self, *,
    node_id, node_fingerprint, mode, structure,
    externalservicecall_id=None,
    summary=None,                  # NEW
    core_concepts=None,            # NEW
    mentioned_concepts=None,       # NEW
    corrections=None,              # NEW
    step_type=None,                # NEW
) -> StructureSnapshot
```

### Acceptance Criteria

- [ ] `StepInput`, `StepOutput`, `NodeSummary`, `StepType` визначені з типами
- [ ] `CourseStructure` має `summary`, `core_concepts`, `mentioned_concepts` з model_validator
- [ ] `StructureSnapshot` ORM має нові nullable колонки + міграція
- [ ] `arq_execute_step` працює для `step_type="generate"` ідентично старому `arq_generate_structure`
- [ ] `ArchitectAgent.execute(StepInput) → StepOutput` працює
- [ ] Існуючі тести проходять без змін (backward compatible)
- [ ] Нові тести для StepInput/StepOutput контрактів
- [ ] Нові тести для `arq_execute_step`

### Файли

| Файл | Дія |
|------|-----|
| `src/course_supporter/models/step.py` | NEW — контракти |
| `src/course_supporter/models/course.py` | EDIT — додати summary, concepts |
| `src/course_supporter/storage/orm.py` | EDIT — нові колонки StructureSnapshot |
| `src/course_supporter/storage/snapshot_repository.py` | EDIT — розширити create() |
| `src/course_supporter/api/tasks.py` | EDIT — arq_execute_step + wrapper |
| `src/course_supporter/agents/architect.py` | EDIT — додати execute() |
| `migrations/versions/xxxx_add_snapshot_step_fields.py` | NEW — міграція |
| `tests/unit/test_step_contracts.py` | NEW |
| `tests/unit/test_step_executor.py` | NEW |

---

## S3-020b: Bottom-up DAG оркестрація та per-node генерація

**Складність:** L
**Залежність:** S3-020a
**Мета:** Orchestrator створює DAG задач для bottom-up генерації (Pass 1). Кожен вузол генерується окремо з children summaries як контекст.

### Що робимо

#### 1. Per-node job DAG в Orchestrator

**Файл:** `src/course_supporter/generation_orchestrator.py`

Нова функція (або рефакторинг `trigger_generation`):
```python
async def trigger_recursive_generation(
    *,
    redis: ArqRedis,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    root_node_id: uuid.UUID,
    target_node_id: uuid.UUID | None = None,
    mode: Literal["free", "guided"] = "free",
    passes: list[StepType] | None = None,        # default: ["generate"]
) -> GenerationPlan:
```

Алгоритм:
1. Завантажити дерево, resolve target, flatten
2. Перевірити конфлікти
3. Обробити stale матеріали (enqueue ingestion)
4. **Post-order traversal** (bottom-up):
   - Для кожного leaf: створити `Job(step_type="generate", depends_on=[ingestion_jobs якщо є])`
   - Для кожного parent: створити `Job(step_type="generate", depends_on=[children_generate_jobs])`
5. Повернути `GenerationPlan` з усіма jobs

#### 2. Розширити enqueue_generation для step_type

**Файл:** `src/course_supporter/enqueue.py`

```python
async def enqueue_step(
    *,
    redis: ArqRedis,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
    step_type: str,                  # "generate" | "reconcile" | "refine"
    mode: str = "free",
    depends_on: list[str] | None = None,
) -> Job:
```

Enqueue `arq_execute_step` замість `arq_generate_structure`.

#### 3. Step Executor: заповнити children_summaries

**Файл:** `src/course_supporter/api/tasks.py`

В `arq_execute_step`, для `step_type="generate"`:
- Завантажити дочірні вузли поточного node
- Для кожного дочірнього вузла: прочитати найсвіжіший `StructureSnapshot` (з `summary`, `core_concepts`, `mentioned_concepts`)
- Побудувати `list[NodeSummary]`
- Передати в `StepInput.children_summaries`

#### 4. ArchitectAgent: використати children_summaries в промпті

**Файл:** `src/course_supporter/agents/architect.py`

В `execute()`:
- Якщо `step_input.children_summaries` непустий → серіалізувати в JSON
- Передати як додатковий контекст в user_prompt

**Файл:** `prompts/architect/v1.yaml` та `v1_guided.yaml`
- Додати placeholder `{children_context}` і інструкції для LLM:
  - "Ці підтеми вже згенеровані. Використай їх підсумки для побудови загальної структури."

#### 5. Розширити GenerationPlan

**Файл:** `src/course_supporter/generation_orchestrator.py`

```python
@dataclass(frozen=True, slots=True)
class GenerationPlan:
    ingestion_jobs: list[Job]
    generation_jobs: list[Job]          # WAS: generation_job (singular)
    reconciliation_jobs: list[Job]      # NEW (порожній поки що)
    existing_snapshot_id: uuid.UUID | None
    is_idempotent: bool
    mapping_warnings: list[MappingWarning]
    estimated_llm_calls: int            # NEW — для cost guard
```

#### 6. Оновити API endpoint

**Файл:** `src/course_supporter/api/routes/generation.py`

Endpoint `POST /nodes/{node_id}/generate` викликає `trigger_recursive_generation()` замість `trigger_generation()`.
Response schema оновити для списку jobs замість одного.

### Acceptance Criteria

- [ ] Orchestrator створює per-node jobs з правильними `depends_on` (bottom-up)
- [ ] Leaf вузли генеруються без залежностей (або після ingestion)
- [ ] Parent вузли чекають на завершення children jobs
- [ ] `arq_execute_step` заповнює `children_summaries` з БД
- [ ] ArchitectAgent використовує children_summaries в промпті
- [ ] `GenerationPlan` містить список jobs + estimated_llm_calls
- [ ] Cascading failure працює через DAG (вже реалізовано S3-019)
- [ ] Інтеграційний тест: дерево з 3 рівнями → правильний порядок jobs

### Файли

| Файл | Дія |
|------|-----|
| `src/course_supporter/generation_orchestrator.py` | EDIT — trigger_recursive_generation |
| `src/course_supporter/enqueue.py` | EDIT — enqueue_step |
| `src/course_supporter/api/tasks.py` | EDIT — children_summaries loading |
| `src/course_supporter/agents/architect.py` | EDIT — children context in prompt |
| `src/course_supporter/api/routes/generation.py` | EDIT — new orchestrator call |
| `prompts/architect/v1.yaml` | EDIT — children_context placeholder |
| `prompts/architect/v1_guided.yaml` | EDIT — children_context placeholder |
| `tests/unit/test_recursive_orchestrator.py` | NEW |
| `tests/unit/test_step_executor_children.py` | NEW |

---

## S3-020c: Reconciliation pass (ковзне вікно)

**Складність:** XL
**Залежність:** S3-020b
**Мета:** Після bottom-up генерації — top-down прохід узгодження. Для кожного вузла формується ковзне вікно з трьох рівнів (батько ← поточний + sibling'и → діти).

### Що робимо

#### 1. ReconcileAgent

**Файл:** `src/course_supporter/agents/reconcile.py` (NEW)

```python
class ReconcileAgent:
    def __init__(self, router: ModelRouter) -> None
    async def execute(self, step_input: StepInput) -> StepOutput
```

Контракт:
- Отримує `StepInput` з `step_type=RECONCILE`
- `parent_context` — summary батька (макро-контекст)
- `sibling_summaries` — summaries сусідів (поточний рівень)
- `children_summaries` — summaries дітей (мікро-контекст)
- Повертає `StepOutput` з `corrections` та `terminology_map`

#### 2. Промпти для reconciliation

**Файл:** `prompts/reconcile/v1.yaml` (NEW)

Промпт включає:
- Контекст батька (якщо є): "Це частина загальної структури: {parent_summary}"
- Поточний вузол + sibling'и: "Порівняй ці паралельні теми: {sibling_structures}"
- Деталізація дітей (якщо є): "Дочірні теми: {children_summaries}"
- Інструкції:
  - Виявити суперечності (conflicting definitions)
  - Виявити прогалини (mentioned_concept без core_concept ніде)
  - Нормалізувати термінологію
  - Перевірити послідовність пререквізитів

#### 3. Orchestrator: top-down reconciliation DAG

**Файл:** `src/course_supporter/generation_orchestrator.py`

Розширити `trigger_recursive_generation()`:
- Якщо `passes` містить `"reconcile"`:
  1. **Pre-order traversal** (top-down)
  2. Для root: `Job(step_type="reconcile", depends_on=[all_generate_jobs])`
  3. Для кожного вузла з дітьми: `Job(step_type="reconcile", depends_on=[parent_reconcile_job])`
  4. Leaf без дітей — reconciliation не потрібна (нема що узгоджувати)

#### 4. Step Executor: побудова ковзного вікна

**Файл:** `src/course_supporter/api/tasks.py`

В `arq_execute_step`, для `step_type="reconcile"`:
1. Завантажити summary батька → `parent_context`
2. Завантажити summaries sibling'ів → `sibling_summaries`
3. Завантажити summaries дітей → `children_summaries`
4. Побудувати `StepInput`
5. Викликати `ReconcileAgent.execute()`

#### 5. Застосування corrections

**Файл:** `src/course_supporter/reconciliation.py` (NEW)

```python
async def apply_corrections(
    session: AsyncSession,
    corrections: list[Correction],
    terminology_map: dict[str, str],
) -> int:
    """Apply reconciliation corrections to StructureNodes."""
```

Corrections зберігаються в snapshot для audit trail. Зміни застосовуються до `structure_nodes`.

#### 6. Pydantic schema для reconciliation output

**Файл:** `src/course_supporter/models/reconciliation.py` (NEW)

```python
class ReconciliationOutput(BaseModel):
    """LLM response schema for reconciliation."""
    corrections: list[CorrectionItem]
    terminology_map: dict[str, str]
    gaps_detected: list[GapItem]
    contradictions_detected: list[ContradictionItem]
```

### Acceptance Criteria

- [ ] `ReconcileAgent` приймає `StepInput` з ковзним вікном і повертає `StepOutput`
- [ ] Ковзне вікно правильно формується: батько + sibling'и + діти
- [ ] Рівні яких немає (root без батька, leaf без дітей) — ігноруються
- [ ] Orchestrator створює top-down DAG для reconciliation
- [ ] Corrections зберігаються в `structure_snapshots.corrections`
- [ ] Corrections можуть застосовуватись до `structure_nodes`
- [ ] Тести для ковзного вікна (різні форми дерева)
- [ ] Тести для ReconcileAgent (mock LLM)

### Файли

| Файл | Дія |
|------|-----|
| `src/course_supporter/agents/reconcile.py` | NEW |
| `src/course_supporter/models/reconciliation.py` | NEW |
| `src/course_supporter/reconciliation.py` | NEW — apply corrections |
| `src/course_supporter/generation_orchestrator.py` | EDIT — reconcile DAG |
| `src/course_supporter/api/tasks.py` | EDIT — sliding window + reconcile dispatch |
| `prompts/reconcile/v1.yaml` | NEW |
| `tests/unit/test_reconcile_agent.py` | NEW |
| `tests/unit/test_sliding_window.py` | NEW |
| `tests/unit/test_reconcile_orchestrator.py` | NEW |

---

## S3-020d: Selective refine pass

**Складність:** M
**Залежність:** S3-020c
**Мета:** Після правок користувача в `structure_nodes` — перегенерація лише зміненого піддерева зі збереженням ручних правок.

### Що робимо

#### 1. RefineAgent

**Файл:** `src/course_supporter/agents/refine.py` (NEW)

```python
class RefineAgent:
    def __init__(self, router: ModelRouter) -> None
    async def execute(self, step_input: StepInput) -> StepOutput
```

Контракт:
- `step_type=REFINE`
- `existing_structure` — JSON поточних StructureNodes (з правками користувача)
- Ковзне вікно: батько + sibling'и + діти (як для reconcile)
- Промпт: "Структура була відредагована. Збережи ручні правки. Гармонізуй зі сусідами."

#### 2. Промпти для refine

**Файл:** `prompts/refine/v1.yaml` (NEW)

Інструкції:
- Зберегти ручні правки (title, description, ordering від користувача)
- Гармонізувати з контекстом сусідів (ковзне вікно)
- Оновити summary та concepts

#### 3. Визначення зміненого піддерева

**Файл:** `src/course_supporter/generation_orchestrator.py`

```python
async def trigger_refine(
    *,
    redis, session, tenant_id,
    node_id: uuid.UUID,            # вузол що змінився
    mode: str = "free",
) -> GenerationPlan:
```

Алгоритм:
1. Визначити змінений вузол
2. Перегенерувати лише цей вузол (refine) + його предків вгору до root (reconcile)
3. Sibling'и не перегенеровуються — лише використовуються як контекст

#### 4. API endpoint

**Файл:** `src/course_supporter/api/routes/generation.py`

```
POST /nodes/{node_id}/refine
```

Запускає selective refine для вузла після ручних правок.

### Acceptance Criteria

- [ ] `RefineAgent` зберігає ручні правки користувача
- [ ] Перегенерація лише зміненого вузла + предків
- [ ] Sibling'и використовуються як контекст, не перегенеровуються
- [ ] Ковзне вікно працює для refine
- [ ] API endpoint `POST /nodes/{node_id}/refine`
- [ ] Тести для RefineAgent (mock LLM)
- [ ] Тести для trigger_refine (правильний scope)

### Файли

| Файл | Дія |
|------|-----|
| `src/course_supporter/agents/refine.py` | NEW |
| `src/course_supporter/generation_orchestrator.py` | EDIT — trigger_refine |
| `src/course_supporter/api/routes/generation.py` | EDIT — POST /refine |
| `src/course_supporter/api/tasks.py` | EDIT — refine dispatch |
| `prompts/refine/v1.yaml` | NEW |
| `tests/unit/test_refine_agent.py` | NEW |
| `tests/unit/test_refine_orchestrator.py` | NEW |

---

## Загальний порядок виконання

```
S3-020a (L)  ─── Контракти + рефакторинг ─── backward compatible
     │
S3-020b (L)  ─── Bottom-up DAG ───────────── нова функціональність, старий endpoint оновлюється
     │
S3-020c (XL) ─── Reconciliation ──────────── нова функціональність, новий Agent
     │
S3-020d (M)  ─── Selective refine ─────────── нова функціональність, новий endpoint
```

**Кожна задача — окремий PR.** Після кожного PR система працює.
Старий `arq_generate_structure` підтримується як wrapper до завершення S3-020b (потім можна видалити).

## Оцінка обсягу

| Задача | Нових файлів | Змінених файлів | Оцінка тестів |
|--------|-------------|----------------|---------------|
| S3-020a | 3 | 6 | ~25 |
| S3-020b | 2 | 7 | ~20 |
| S3-020c | 4 | 3 | ~25 |
| S3-020d | 2 | 3 | ~15 |
| **Разом** | **11** | **~12 унікальних** | **~85** |
