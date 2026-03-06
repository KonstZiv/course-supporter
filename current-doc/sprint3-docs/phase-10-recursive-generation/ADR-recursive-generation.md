# ADR: Рекурсивний pipeline генерації через LLM

**Статус:** PROPOSED
**Дата:** 2026-03-06
**Задача:** S3-020

---

## 1. Загальний підхід

### Проблема

Поточний pipeline генерації розглядає все дерево `MaterialNode` як єдиний блок: усі матеріали зливаються в один `CourseContext`, відправляються одним LLM-запитом, а результат зберігається як один `StructureSnapshot`. Це працює для невеликих курсів, але має фундаментальні обмеження:

- **Ліміт context window.** Великі дерева з багатьма матеріалами можуть перевищити ліміт токенів LLM.
- **Відсутність інкрементальної генерації.** Зміна одного листка змушує перегенерувати все дерево.
- **Відсутність крос-вузлової обізнаності.** LLM не може враховувати зв'язки між сусідніми вузлами, контекст батьківського вузла чи ланцюжки пререквізитів.
- **Відсутність узгодження.** Незалежні генерації можуть створювати суперечності, неузгодженість термінології та прогалини.

### Рішення: DAG-based Multi-pass Generation

Розбити генерацію на **DAG кроків** (Directed Acyclic Graph — направлений ациклічний граф), де кожен крок:

1. Має чітко визначений **вхідний контракт** (які дані отримує).
2. Створює чітко визначений **вихідний контракт** (що повертає).
3. **Незалежний від оркестрації** — не знає про jobs, черги чи сам DAG.

Система працює у три проходи:

| Прохід | Напрямок | Мета |
|--------|----------|------|
| **Generate** | Знизу вгору | Кожен вузол генерує свою структуру з власних матеріалів + summaries дітей |
| **Reconcile** | Зверху вниз | Виявлення суперечностей, прогалин, нормалізація термінології між сусідами |
| **Refine** | Вибірково | Перегенерація зміненого піддерева після правок користувача зі збереженням ручних змін |

---

## 2. Розділення відповідальності

Архітектура має чотири шари. Кожен шар має єдину відповідальність і спілкується з іншими лише через визначені контракти.

```
+-----------------------------------------------------------+
|                     API Layer                              |
|  Отримує запит користувача, повертає GenerationPlan        |
+-----------------------------+-----------------------------+
                              |
+-----------------------------v-----------------------------+
|                   Orchestrator                             |
|  Будує DAG кроків. Створює Jobs з depends_on.              |
|  Визначає порядок виконання. НЕ викликає LLM.             |
+-----------------------------+-----------------------------+
                              |
+-----------------------------v-----------------------------+
|                   Step Executor                            |
|  Виконує один крок: завантажує input, викликає Agent,      |
|  зберігає output. Керує lifecycle Job (active → complete). |
|  НЕ знає про інші кроки чи DAG.                           |
+-----------------------------+-----------------------------+
                              |
+-----------------------------v-----------------------------+
|                      Agent                                 |
|  Чиста взаємодія з LLM. Отримує підготовлений контекст,   |
|  повертає structured output. Без БД, без jobs, без дерева. |
+-----------------------------------------------------------+
```

### Деталі шарів

**Orchestrator** — знає повне дерево, вирішує що генерувати і в якому порядку.
- Вхід: root_node_id, target_node_id, mode
- Вихід: `GenerationPlan` (список Jobs з ланцюжками залежностей)
- Відповідальність: обхід дерева, перевірка fingerprints, idempotency, створення jobs
- НЕ робить: виклик LLM, читання/запис контенту, керування статусом jobs

**Step Executor** (ARQ task) — виконує один крок генерації/узгодження.
- Вхід: job_id, node_id, step_type
- Вихід: StructureSnapshot + StructureNodes зберігаються в БД
- Відповідальність: завантаження даних вузла, побудова StepInput, виклик Agent, збереження StepOutput, оновлення Job
- НЕ робить: не знає про інші вузли в DAG, не створює jobs, не вирішує порядок виконання

**Agent** — чиста LLM-обгортка з підготовкою промптів.
- Вхід: `StepInput` (підготовлений контекст для конкретного кроку)
- Вихід: `StepOutput` (структурований результат LLM + metadata)
- Відповідальність: форматування промпту, виклик LLM, парсинг відповіді, retry при невалідному JSON
- НЕ робить: не торкається БД, не знає про jobs, не знає про структуру дерева

---

## 3. Приклад роботи

### Вхідне дерево

```
Root (MaterialNode)
+-- Topic A
|   +-- Subtopic A1 (має 2 матеріали: video + slides)
|   +-- Subtopic A2 (має 1 матеріал: text)
+-- Topic B (має 1 матеріал: web link)
```

### Прохід 1: Генерація знизу вгору (bottom-up)

```
          Root
         /    \
    Topic A   Topic B
    /    \
  A1      A2

Порядок виконання (bottom-up):
  Рівень 2: A1, A2, B  (паралельно, без залежностей)
  Рівень 1: A           (depends_on: [job_A1, job_A2])
  Рівень 0: Root        (depends_on: [job_A, job_B])
```

#### Крок 1a: Генерація A1 (листковий вузол)

```
StepInput:
  node_id: A1
  step_type: "generate"
  materials: [video_doc, slides_doc]     # з MaterialEntries вузла A1
  children_summaries: []                  # листковий вузол, дітей немає
  parent_context: null                    # перший прохід, батька ще немає
  sibling_summaries: []                   # перший прохід, сусідів ще немає

     |  Agent (LLM-запит)
     v

StepOutput:
  structure: CourseStructure(modules=[...])  # уроки/концепти для A1
  summary: "Covers Python installation..."   # автогенерований підсумок
  core_concepts: ["installing Python", "IDE setup"]
  mentioned_concepts: ["variables", "print function"]
  metadata: LLMResponse(tokens, cost, model)
```

#### Крок 1b: Генерація A (батьківський вузол, після завершення A1 + A2)

```
StepInput:
  node_id: A
  step_type: "generate"
  materials: []                              # A не має власних матеріалів
  children_summaries: [
    {node_id: A1, title: "Subtopic A1", summary: "Covers Python installation..."},
    {node_id: A2, title: "Subtopic A2", summary: "Covers variables and types..."},
  ]
  parent_context: null
  sibling_summaries: []

     |  Agent (LLM-запит)
     v

StepOutput:
  structure: CourseStructure(modules=[...])  # структура A на основі дітей
  summary: "Introduction to Python basics..."
  metadata: LLMResponse(...)
```

#### Крок 1c: Генерація Root (після завершення A + B)

```
StepInput:
  node_id: Root
  step_type: "generate"
  materials: []
  children_summaries: [
    {node_id: A, summary: "Introduction to Python basics..."},
    {node_id: B, summary: "Web scraping fundamentals..."},
  ]

     |  Agent (LLM-запит)
     v

StepOutput:
  structure: CourseStructure(...)  # повна структура курсу
  summary: "Complete Python course..."
```

### Прохід 2: Узгодження зверху вниз (top-down reconciliation)

```
Порядок виконання (top-down):
  Рівень 0: Root       (без залежностей)
  Рівень 1: A          (depends_on: [reconcile_Root])
  Рівень 2: A1, A2, B  (depends_on: [reconcile_A] або [reconcile_Root])
```

#### Крок 2a: Узгодження Root (ковзне вікно: Root + діти A, B)

```
StepInput (ковзне вікно для Root):
  node_id: Root
  step_type: "reconcile"

  # Верхній рівень — батько Root (немає, це корінь)
  parent_context: null

  # Поточний рівень — Root + sibling'и (немає, Root єдиний)
  own_structure: CourseStructure вузла Root
  sibling_summaries: []

  # Нижній рівень — summaries дітей Root
  children_summaries: [
    {node_id: A, summary: "Intro to Python...", core_concepts: ["variables", "types"]},
    {node_id: B, summary: "Web scraping...", core_concepts: ["requests", "BeautifulSoup"]},
  ]

     |  Agent (LLM-запит — промпт для виявлення суперечностей/прогалин)
     v

StepOutput:
  corrections: [
    {node_id: A, field: "title", old: "Python Intro", new: "Python Fundamentals"},
    {node_id: B, action: "add_prerequisite", concept: "variables"},
  ]
  terminology_map: {"variable": "variable", "var": "variable"}
```

#### Крок 2b: Узгодження A (ковзне вікно: Root ← A + B → A1, A2)

```
StepInput (ковзне вікно для A):
  node_id: A
  step_type: "reconcile"

  # Верхній рівень — summary батька (Root, вже узгоджений)
  parent_context: {node_id: Root, summary: "Complete Python course..."}

  # Поточний рівень — A + sibling B
  own_structure: CourseStructure вузла A
  sibling_summaries: [
    {node_id: B, summary: "Web scraping...", core_concepts: ["requests"]},
  ]

  # Нижній рівень — summaries дітей A
  children_summaries: [
    {node_id: A1, summary: "Python installation...", core_concepts: ["installing Python"]},
    {node_id: A2, summary: "Variables and types...", core_concepts: ["variables", "types"]},
  ]

     |  Agent (LLM-запит)
     v

StepOutput:
  corrections: [...]
  terminology_map: {...]
```

### DAG задач (повна картина)

```
                    [generate_A1]---+
                                    |
                    [generate_A2]---+-->[generate_A]--+
                                                      |
                    [generate_B]----------------------+-->[generate_Root]
                                                                |
                                                     [reconcile_Root]
                                                         |
                                                     [reconcile_A]
                                                      /        \
                                              [reconcile_A1] [reconcile_A2]
```

Кожен блок — це запис `Job` у БД. Стрілки відображають зв'язки `depends_on`.
Якщо `generate_A1` впаде, каскадний збій пропагується до `generate_A` → `generate_Root` → усі reconciliation jobs.

---

## 4. Контракти компонентів

### 4.1 StepInput — що отримує крок

```python
@dataclass(frozen=True)
class StepInput:
    """Immutable input for a single generation/reconciliation step."""

    node_id: uuid.UUID
    step_type: StepType                          # "generate" | "reconcile" | "refine"

    # Сирі дані (з MaterialEntries)
    materials: list[SourceDocument]              # оброблені матеріали цього вузла

    # Контекст з інших вузлів (заповнюється Step Executor)
    children_summaries: list[NodeSummary]        # результати генерації дітей
    parent_context: NodeSummary | None           # підсумок батька (для reconcile/refine)
    sibling_summaries: list[NodeSummary]         # підсумки сусідів (для reconcile)

    # Існуюча структура (для guided/refine режимів)
    existing_structure: str | None               # JSON поточних StructureNodes

    # Параметри генерації
    mode: Literal["free", "guided"]
    material_tree: list[MaterialNodeSummary]     # метадані дерева для контексту LLM


class StepType(StrEnum):
    GENERATE = "generate"
    RECONCILE = "reconcile"
    REFINE = "refine"


@dataclass(frozen=True)
class NodeSummary:
    """Compact representation of a node's generation result."""
    node_id: uuid.UUID
    title: str
    summary: str                                 # LLM-згенерований підсумок структури
    core_concepts: list[str]                     # концепти, детально розкриті у вузлі
    mentioned_concepts: list[str]                # концепти, згадані поверхнево
    structure_snapshot_id: uuid.UUID | None       # для зв'язування
```

### 4.2 StepOutput — що створює крок

```python
@dataclass(frozen=True)
class StepOutput:
    """Immutable output from a single generation/reconciliation step."""

    structure: CourseStructure                    # розпарсена відповідь LLM
    summary: str                                 # автогенерований підсумок для батьківського контексту
    core_concepts: list[str]                     # концепти, детально розкриті у вузлі
    mentioned_concepts: list[str]                # концепти, згадані поверхнево

    # LLM metadata
    prompt_version: str
    response: LLMResponse                        # provider, model, tokens, cost, latency

    # Специфічно для reconciliation (None для generate кроків)
    corrections: list[Correction] | None
    terminology_map: dict[str, str] | None


@dataclass(frozen=True)
class Correction:
    """A single correction suggested by reconciliation."""
    target_node_id: uuid.UUID
    field: str
    action: str                                  # "rename" | "add" | "remove" | "move"
    old_value: str | None
    new_value: str | None
    reason: str
```

### 4.3 Orchestrator

```
Відповідальність: побудувати DAG задач (Jobs) для запитаної генерації.

Вхід:
  - root_node_id: UUID
  - target_node_id: UUID | None (None = все дерево)
  - mode: "free" | "guided"
  - passes: list[StepType] (за замовчуванням: ["generate", "reconcile"])

Вихід:
  - GenerationPlan (список Jobs з ланцюжками depends_on)

Алгоритм:
  1. Завантажити дерево (MaterialNodeRepository.get_subtree)
  2. Визначити цільове піддерево
  3. Перевірити наявність активних конфліктуючих jobs
  4. Для проходу "generate":
     a. Обхід дерева знизу вгору (post-order)
     b. Для кожного вузла: створити Job(step_type="generate", depends_on=[children_jobs])
  5. Для проходу "reconcile":
     a. Обхід дерева зверху вниз (pre-order)
     b. Для кожного вузла з дітьми: створити Job(step_type="reconcile",
        depends_on=[parent_reconcile + all_generate])
  6. Обробити stale матеріали (спочатку enqueue ingestion)
  7. Повернути GenerationPlan

НЕ робить:
  - Виклик LLM
  - Читання контенту матеріалів
  - Керування переходами статусів jobs
```

### 4.4 Step Executor (ARQ task)

```
Відповідальність: виконати один крок — завантажити дані, викликати Agent, зберегти результат.

Вхід (з Job):
  - job_id: UUID
  - node_id: UUID
  - step_type: StepType

Алгоритм:
  1. Перевести job у статус "active"
  2. Завантажити вузол + матеріали з БД
  3. Побудувати ковзне вікно контексту:
     - Завантажити summaries дітей з їхніх StructureSnapshots
     - Завантажити summary батька (якщо є, для reconcile/refine)
     - Завантажити summaries sibling'ів (якщо є, для reconcile)
  4. Побудувати StepInput
  5. Обрати та викликати відповідний Agent (GenerateAgent / ReconcileAgent)
  6. Отримати StepOutput
  7. Зберегти: StructureSnapshot + StructureNodes + ExternalServiceCall
  8. Зберегти NodeSummary для споживання батьківськими кроками
  9. Перевести job у статус "complete"
  При помилці:
  10. Перевести job у статус "failed"
  11. Пропагувати збій до залежних jobs

НЕ робить:
  - Не знає про інші кроки в DAG
  - Не створює нових jobs
  - Не вирішує порядок виконання
```

### 4.5 Agent (взаємодія з LLM)

```
Відповідальність: підготувати промпт, викликати LLM, розпарсити відповідь.

Вхід: StepInput
Вихід: StepOutput

Варіанти:
  - GenerateAgent: генерує CourseStructure з матеріалів + summaries дітей
  - ReconcileAgent: виявляє суперечності/прогалини між структурами сусідів
  - RefineAgent: перегенерує піддерево зі збереженням правок користувача

Кожен agent:
  1. Обирає шаблон промпту за step_type + mode
  2. Форматує промпт даними з StepInput
  3. Викликає ModelRouter.complete_structured()
  4. Парсить та валідує відповідь
  5. Витягує summary та key_concepts
  6. Повертає StepOutput

НЕ робить:
  - Не звертається до БД
  - Не знає про jobs чи оркестрацію
  - Не керує станом чи side effects
```

---

## 5. Потік даних між проходами

### Де що зберігається

| Дані | Сховище | Час життя |
|------|---------|-----------|
| Сирі матеріали | `material_entries.processed_content` | Постійно (до переобробки) |
| Вхідний контекст кроку | Збирається в пам'яті Step Executor | Тимчасово (на час виконання кроку) |
| Сира відповідь LLM | `structure_snapshots.structure` (JSONB) | Постійно (immutable) |
| Редагована структура | дерево `structure_nodes` | Постійно (мутабельне користувачем) |
| Підсумок вузла | `structure_snapshots.summary` (нове поле) | Постійно (використовується ковзним вікном) |
| Core concepts | `structure_snapshots.core_concepts` (новий JSONB) | Постійно (для крос-референсів та gap detection) |
| Mentioned concepts | `structure_snapshots.mentioned_concepts` (новий JSONB) | Постійно (для gap detection) |
| Метадані задачі | таблиця `jobs` | Постійно |
| Трекінг вартості LLM | таблиця `external_service_calls` | Постійно |
| Корекції | `structure_snapshots.corrections` (новий JSONB) | Постійно (аудит reconciliation) |

### Передача даних між кроками

Кроки НЕ спілкуються безпосередньо. Step Executor читає результати попередніх кроків з бази даних:

```
generate_A1 завершується
  → StepOutput зберігається в structure_snapshots (snapshot вузла A1)
  → NodeSummary зберігається (summary + key_concepts)

generate_A стартує (depends_on: [generate_A1, generate_A2])
  → Step Executor читає NodeSummary A1 з БД
  → Step Executor читає NodeSummary A2 з БД
  → Будує StepInput з children_summaries
  → Викликає Agent
```

Це означає:
- Відсутність in-memory стану між кроками (ARQ workers можуть бути різними процесами)
- Кожен крок можна незалежно повторити (retry)
- Додавання нового типу проходу потребує лише: новий варіант Agent + новий промпт + оновлення orchestrator

---

## 6. Шлях міграції з поточної архітектури

### Що залишається без змін
- `MaterialNode` / `MaterialEntry` — зберігання сирих даних
- `FingerprintService` — перевірки idempotency
- `MergeStep` — сортування документів та cross-referencing (використовується всередині Step Executor)
- `structure_conversion` — маппінг CourseStructure → StructureNode
- `Job` модель з `depends_on` — вже підтримує DAG
- `propagate_failure` — каскадний збій вже реалізований

### Що змінюється
- `generation_orchestrator.trigger_generation()` — розширення для створення per-node job DAGs
- `arq_generate_structure` — рефакторинг у generic Step Executor
- `ArchitectAgent` — обгортка в абстракцію Agent з StepInput/StepOutput
- Нове: `ReconcileAgent` з новими промптами
- Нове: `StepInput` / `StepOutput` / `NodeSummary` data classes
- Нове: колонки `structure_snapshots.summary`, `.core_concepts`, `.mentioned_concepts`, `.corrections`
- Розширення `CourseStructure`: нові поля `summary`, `core_concepts`, `mentioned_concepts` у response schema

### Інкрементальний порядок імплементації
1. **S3-020a**: Визначити контракти `StepInput` / `StepOutput` / `NodeSummary`. Рефакторити `arq_generate_structure` для їх використання (один вузол, без DAG).
2. **S3-020b**: Розширити orchestrator для bottom-up створення DAG задач. Per-node генерація з children summaries.
3. **S3-020c**: Додати `ReconcileAgent` + промпти для reconciliation. Top-down прохід узгодження.
4. **S3-020d**: Вибіркова перегенерація (refine прохід після правок користувача).

---

## 7. Прийняті рішення

### 7.1 Гранулярність snapshots

**Рішення: один snapshot на вузол на кожен прохід (immutable audit trail).**

Кожен прохід (generate, reconcile, refine) створює НОВИЙ запис у `structure_snapshots`.
Для вузла A1 після повного pipeline в БД буде 3 записи — по одному на кожен прохід.
Актуальна версія — snapshot з найбільшим `created_at` для даного `node_id`.

Оцінка об'єму: навіть для 10 000 курсів × 100 вузлів × 3 проходи = 3 000 000 записів —
це штатне навантаження для PostgreSQL з JSONB. Індекс на `(node_id, created_at DESC)`
забезпечує швидку вибірку актуального snapshot.

### 7.2 Генерація summary та концептів

**Рішення: LLM генерує summary і концепти як частину structured output.**

Розширити `CourseStructure` (response schema) трьома додатковими полями:

- `summary: str` — стислий підсумок згенерованої структури (для батьківського контексту)
- `core_concepts: list[str]` — концепти, які **детально розкриваються** у цьому вузлі
- `mentioned_concepts: list[str]` — концепти, які **згадуються поверхнево** (пререквізити, посилання)

LLM найкраще підходить для підсумовування того, що сам згенерував. Розділення концептів
на core/mentioned дозволяє reconciliation виявляти прогалини (concept згадується в одному
вузлі як mentioned, але ніде не розкривається як core).

**Інваріант:** множини `core_concepts` та `mentioned_concepts` не перетинаються —
кожен концепт потрапляє лише в одну з двох множин.
`bool(set(mentioned_concepts) & set(core_concepts)) is False`.

### 7.3 Scope reconciliation: ковзне вікно

**Рішення: ковзне вікно з трьох рівнів для кожного вузла.**

Для кожного вузла, що проходить reconciliation, формується контекст з трьох рівнів:

```
        [батьківський рівень]     ← summary батька (якщо є)
               |
   [sibling-1] [ВУЗОЛ] [sibling-2]  ← повна структура поточного + сусідів
               |
     [child-1] [child-2]            ← summaries дітей (якщо є)
```

Вікно ковзає по дереву — для кожного вузла центрується на ньому і захоплює:

- **Зверху** (макро-контекст): summary батька — розуміння загальної картини "згори".
  Якщо батька немає (root) — ігнорується.
- **Поточний рівень**: сам вузол + усі його sibling'и — повні структури для виявлення
  суперечностей та прогалин між ними.
- **Знизу** (мікро-контекст): summaries дітей поточного вузла — деталізація "знизу".
  Якщо дітей немає (leaf) — ігнорується.

Ковзне вікно працює однаково для обох проходів:

- **Generate (bottom-up):** summaries дітей вже є (згенеровані раніше), sibling'и
  паралельного рівня можуть бути доступні, батько ще не згенерований → `null`.
- **Reconcile (top-down):** summary батька вже є (узгоджений раніше), поточний рівень
  та summaries дітей доступні з generate проходу.

З кожним проходом вікно стає інформативнішим — на generate ми бачимо лише "знизу",
на reconcile вже маємо контекст з обох боків.

### 7.4 Cost guard

**Рішення: GenerationPlan повертає estimated cost, можливість підтвердження передбачена.**

Orchestrator повертає у `GenerationPlan` оцінку вартості (кількість LLM-запитів,
приблизна кількість токенів на основі розміру матеріалів). API layer може вимагати
підтвердження користувача для великих DAGs. Конкретна логіка підтвердження визначається
на рівні API — Orchestrator лише надає дані для прийняття рішення.
