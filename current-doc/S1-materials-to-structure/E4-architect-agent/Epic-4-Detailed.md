# Epic 4: Architect Agent (Методист) ✅

## Мета

AI-агент, що аналізує `CourseContext` (результат Ingestion) і генерує структуровану навчальну програму курсу. Після цього епіку — система перетворює сирі матеріали на повний навчальний план: модулі → уроки → концепції з cross-references на таймкоди/слайди/URL + практичні завдання.

## Передумови (виконано)

- **Epic 1 ✅**: DB schema готова — таблиці `modules`, `lessons`, `concepts`, `exercises` з FK constraints та JSONB полями.
- **Epic 2 ✅**: `ModelRouter` готовий. Action `course_structuring` (requires: structured_output) вже в `config/models.yaml`. Chains: default (`gemini-2.5-flash → deepseek-chat`), quality (`claude-sonnet → gemini-2.5-pro`), budget (`deepseek-chat`).
- **Epic 3 ✅**: Ingestion pipeline готовий — SourceProcessors → MergeStep → `CourseContext`. 101 тест.

## Що зроблено

Чотири компоненти:

1. **Pydantic-моделі output** ([S1-019](T1-course-structure/T019-course-structure.md)) — `CourseStructure`, `ModuleOutput`, `LessonOutput`, `ConceptOutput`, `ExerciseOutput`, `SlideRange`, `WebReference`, `ModuleDifficulty`. Повний набір типізованих схем для structured output агента. Суфікс `Output` для уникнення collision з ORM. Моделі розширені learning-полями: `learning_goal`, `expected_knowledge`, `expected_skills` на рівні курсу та модуля.
2. **System prompt v1 + prompt_loader** ([S1-020](T2-architect-prompt/T020-architect-prompt.md)) — педагогічний промпт в `prompts/architect/v1.yaml` з goal-driven decomposition, knowledge/skills separation, progressive ordering. `PromptData` (Pydantic model) замість raw dict для валідації YAML. `prompt_loader.py` з `load_prompt()` і `format_user_prompt()`.
3. **ArchitectAgent клас** ([S1-021](T3-architect-agent/T021-architect-agent.md)) — step-based архітектура: `_prepare_prompts()` (sync) → `_generate()` (async) з `PreparedPrompt` NamedTuple як проміжний тип. Готовий для майбутньої міграції на LangGraph/DAG. Token-оптимізований: `model_dump_json()` без indent.
4. **Збереження структури** ([S1-022](T4-save-course/T022-save-course.md)) — `CourseStructureRepository` з replace-стратегією: `modules.clear()` + cascade delete + створення нових. Маппінг learning-полів в ORM + Alembic migration для нових колонок.

## Фінальна структура

```
src/course_supporter/agents/
├── __init__.py           # Public: ArchitectAgent, PreparedPrompt, PromptData, load_prompt, format_user_prompt
├── architect.py          # ArchitectAgent (step-based: _prepare_prompts → _generate)
└── prompt_loader.py      # PromptData (Pydantic), load_prompt(path), format_user_prompt(template, context)

src/course_supporter/models/
└── course.py             # +7 output models: CourseStructure, ModuleOutput, LessonOutput, ConceptOutput,
                          #   ExerciseOutput, SlideRange, WebReference + ModuleDifficulty type alias

src/course_supporter/storage/
└── repositories.py       # +CourseStructureRepository (save + _create_module/lesson/concept/exercise)
└── orm.py                # +learning columns: Course(learning_goal, expected_knowledge, expected_skills),
                          #   Module(description, learning_goal, expected_knowledge, expected_skills, difficulty)

prompts/architect/
└── v1.yaml               # Pedagogical system prompt + user prompt template (version: "1.0")

migrations/versions/
└── d2283f3d7212_*.py     # Migration: learning fields for courses and modules
```

## Контрольні точки

- [x] Pydantic-моделі: `CourseStructure` серіалізується/десеріалізується без втрат — 16 тестів
- [x] System prompt: педагогічний підхід (goal-driven, knowledge vs skills, progressive ordering) — 12 тестів
- [x] ArchitectAgent: step-based pipeline CourseContext → PreparedPrompt → CourseStructure — 11 тестів
- [x] Structured output: LLM response валідується Pydantic, при невалідному — retry/fallback (ModelRouter)
- [x] Persistence: CourseStructure → DB зберігається транзакційно (flush, not commit) — 16 тестів
- [x] Re-generation: повторний виклик замінює попередню структуру (replace strategy: clear + cascade)
- [x] Unit-тести з mocked LLM responses — **55 тестів** (16+12+11+16)
- [x] `make check` проходить

## Залежності

- **Блокується:** Epic 2 (ModelRouter) ✅, Epic 3 (CourseContext з Ingestion) ✅
- **Порядок імплементації (виконано):**
  1. S1-019 (Pydantic-моделі) — блокувало все інше
  2. S1-020 (Prompt + loader) — залежало від S1-019
  3. S1-021 (ArchitectAgent) — залежало від S1-019 + S1-020
  4. S1-022 (Persistence) — залежало від S1-019
- **Блокує:** Epic 5 (API endpoint `POST /courses` оркеструє Ingestion → ArchitectAgent → Save)

## Задачі

| ID | Назва | Статус | Тести | Примітка |
|:---|:---|:---|:---|:---|
| S1-019 | Pydantic-моделі output | ✅ | 16 | [spec](T1-course-structure/T019-course-structure.md) · [issue](T1-course-structure/T019-github-issue.md) — 7 моделей + `ModuleDifficulty`, learning fields |
| S1-020 | System prompt v1 + prompt_loader | ✅ | 12 | [spec](T2-architect-prompt/T020-architect-prompt.md) · [issue](T2-architect-prompt/T020-github-issue.md) — `PromptData` Pydantic, YAML + loader |
| S1-021 | ArchitectAgent клас | ✅ | 11 | [spec](T3-architect-agent/T021-architect-agent.md) · [issue](T3-architect-agent/T021-github-issue.md) — step-based, `PreparedPrompt` NamedTuple |
| S1-022 | Збереження структури курсу | ✅ | 16 | [spec](T4-save-course/T022-save-course.md) · [issue](T4-save-course/T022-github-issue.md) — replace strategy, learning fields ORM + migration |

**Загалом: 55 тестів**

---

## Прийняті архітектурні рішення

### 1. Output суфікс для Pydantic-моделей

`ExerciseOutput`, `ConceptOutput`, `LessonOutput`, `ModuleOutput` — суфікс `Output` для уникнення name collision з ORM класами (`Exercise`, `Concept`, `Lesson`, `Module` в `storage/orm.py`). `CourseStructure` та `SlideRange` не конфліктують.

### 2. Learning-oriented fields

`CourseStructure` та `ModuleOutput` розширені педагогічними полями:
- `learning_goal: str` — мета навчання (вимірювана)
- `expected_knowledge: list[str]` — що студент повинен ЗНАТИ після
- `expected_skills: list[str]` — що студент повинен ВМІТИ після
- `difficulty: Literal["easy", "medium", "hard"]` — відносна складність модуля (тільки Module)

Відповідні колонки додані до ORM (`Course` та `Module`) з Alembic migration `d2283f3d7212`.

### 3. Step-based ArchitectAgent

Архітектура з окремими кроками для майбутньої міграції на chain/graph:
- `_prepare_prompts(context) → PreparedPrompt` — sync, завантажує YAML, форматує prompt
- `_generate(prepared) → CourseStructure` — async, виклик LLM через ModelRouter

`PreparedPrompt` (NamedTuple) — проміжний тип між кроками, майбутній кандидат для GraphState.

### 4. PromptData Pydantic model

Замість raw `dict[str, Any]` від `yaml.safe_load()` використовується `PromptData(BaseModel)` з полями `version`, `system_prompt`, `user_prompt_template`. Валідація при завантаженні, type safety без `type: ignore`.

### 5. Replace-стратегія для persistence

При повторному генеруванні `CourseStructureRepository.save()` видаляє існуючі модулі (cascade delete → lessons → concepts → exercises) і створює нові. Простіше ніж diff/merge і достатньо для MVP.

### 6. flush() замість commit()

Як і `SourceMaterialRepository` — caller контролює transaction boundary. При помилці — rollback відміняє все.

### 7. Prompt versioning у YAML

`prompts/architect/v1.yaml` з полем `version: "1.0"`. Для A/B тестування — `v2.yaml`, `v3.yaml`. Prompt loader відповідає за завантаження, ArchitectAgent приймає `prompt_path` як параметр.

### 8. Empty lists / strings → None для DB

ORM JSONB та Text fields nullable. Pydantic default — empty list/string. При persistence: empty `[]` → `None`, empty `""` → `None` (через `or None`) для DB consistency.

### 9. Token optimization

`model_dump_json()` без `indent` для зменшення token usage при відправці CourseContext до LLM. LLM парсить compact JSON коректно.

---

## Shared Pydantic Models (S1-019)

Ці моделі використовуються всіма задачами Epic 4:

```python
# models/course.py (додано до існуючого SlideVideoMapEntry + CourseContext)

ModuleDifficulty = Literal["easy", "medium", "hard"]

class SlideRange(BaseModel):
    start: int
    end: int

class WebReference(BaseModel):
    url: str
    title: str = ""
    description: str = ""

class ExerciseOutput(BaseModel):
    description: str
    reference_solution: str | None = None
    grading_criteria: str | None = None
    difficulty_level: int = Field(default=3, ge=1, le=5)

class ConceptOutput(BaseModel):
    title: str
    definition: str
    examples: list[str] = Field(default_factory=list)
    timecodes: list[str] = Field(default_factory=list)
    slide_references: list[int] = Field(default_factory=list)
    web_references: list[WebReference] = Field(default_factory=list)

class LessonOutput(BaseModel):
    title: str
    video_start_timecode: str | None = None
    video_end_timecode: str | None = None
    slide_range: SlideRange | None = None
    concepts: list[ConceptOutput] = Field(default_factory=list)
    exercises: list[ExerciseOutput] = Field(default_factory=list)

class ModuleOutput(BaseModel):
    title: str
    description: str = ""
    learning_goal: str = ""
    expected_knowledge: list[str] = Field(default_factory=list)
    expected_skills: list[str] = Field(default_factory=list)
    difficulty: ModuleDifficulty = "medium"
    lessons: list[LessonOutput] = Field(default_factory=list)

class CourseStructure(BaseModel):
    title: str
    description: str = ""
    learning_goal: str = ""
    expected_knowledge: list[str] = Field(default_factory=list)
    expected_skills: list[str] = Field(default_factory=list)
    modules: list[ModuleOutput] = Field(default_factory=list)
```

---

## Task Specs

Детальні специфікації для кожної задачі:

```
E4-architect-agent/
├── T1-course-structure/     S1-019: Pydantic output models ✅
├── T2-architect-prompt/     S1-020: System prompt v1 + loader ✅
├── T3-architect-agent/      S1-021: ArchitectAgent class (step-based) ✅
└── T4-save-course/          S1-022: CourseStructureRepository ✅
```

Кожна папка містить `T0XX-*.md` (повна spec з кодом і тестами) + `T0XX-github-issue.md` (summary).
