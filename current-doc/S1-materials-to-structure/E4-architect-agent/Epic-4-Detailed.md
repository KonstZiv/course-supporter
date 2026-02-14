# Epic 4: Architect Agent (Методист)

## Мета

AI-агент, що аналізує `CourseContext` (результат Ingestion) і генерує структуровану навчальну програму курсу. Після цього епіку — система перетворює сирі матеріали на повний навчальний план: модулі → уроки → концепції з cross-references на таймкоди/слайди/URL + практичні завдання.

## Передумови (виконано)

- **Epic 1 ✅**: DB schema готова — таблиці `modules`, `lessons`, `concepts`, `exercises` з FK constraints та JSONB полями.
- **Epic 2 ✅**: `ModelRouter` готовий. Action `course_structuring` (requires: structured_output) вже в `config/models.yaml`. Chains: default (`gemini-2.5-flash → deepseek-chat`), quality (`claude-sonnet → gemini-2.5-pro`), budget (`deepseek-chat`).
- **Epic 3 ✅**: Ingestion pipeline готовий — SourceProcessors → MergeStep → `CourseContext`. 101 тест.
- **Existing stubs**: `agents/architect.py` (TODO), `prompts/architect/v1.yaml` (TODO), `models/course.py` (має `SlideVideoMapEntry`, `CourseContext`), `storage/repositories.py` (має `SourceMaterialRepository`).

## Що робимо

Чотири компоненти:

1. **Pydantic-моделі output** ([S1-019](T1-course-structure/T019-course-structure.md)) — `CourseStructure`, `ModuleOutput`, `LessonOutput`, `ConceptOutput`, `ExerciseOutput`, `SlideRange`, `WebReference`. Повний набір типізованих схем для structured output агента. Суфікс `Output` для уникнення collision з ORM. Ці моделі використовуються і як response schema для LLM, і як API response.
2. **System prompt v1** ([S1-020](T2-architect-prompt/T020-architect-prompt.md)) — промпт для Architect Agent в `prompts/architect/v1.yaml` + `prompt_loader.py`. Інструкції: як аналізувати CourseContext, яку ієрархію генерувати, як створювати concept cards із cross-references. Версіонується для A/B тестування.
3. **ArchitectAgent клас** ([S1-021](T3-architect-agent/T021-architect-agent.md)) — `async def run(context: CourseContext) -> CourseStructure`. Виклик LLM через ModelRouter (action="course_structuring"), валідація output через Pydantic, retry при невалідному JSON або неповній структурі.
4. **Збереження структури** ([S1-022](T4-save-course/T022-save-course.md)) — `CourseStructureRepository`: маппінг `CourseStructure` (Pydantic) → ORM-моделі (modules, lessons, concepts, exercises). Транзакційне збереження з FK constraints, replace-стратегія при повторному генеруванні.

## Для чого

Architect Agent — ядро бізнес-логіки проєкту. Саме він перетворює "купу матеріалів" на "структурований курс". Якість його output визначає цінність усього продукту. Prompt engineering тут — ключова робота.

## Контрольні точки

- [ ] Pydantic-моделі: `CourseStructure` серіалізується/десеріалізується без втрат
- [ ] System prompt: чітко описує що, як і в якому форматі генерувати
- [ ] ArchitectAgent: приймає CourseContext → повертає валідну CourseStructure через ModelRouter
- [ ] Structured output: LLM response валідується Pydantic, при невалідному — retry/fallback
- [ ] Persistence: CourseStructure → DB зберігається транзакційно (all-or-nothing)
- [ ] Re-generation: повторний виклик замінює попередню структуру (не дублює)
- [ ] Unit-тести з mocked LLM responses — **~40 тестів** (10+8+10+12)
- [ ] `make check` проходить

## Залежності

- **Блокується:** Epic 2 (ModelRouter) ✅, Epic 3 (CourseContext з Ingestion) ✅
- **Частковий паралелізм:** S1-019 (Pydantic-моделі) не залежить від Ingestion — можна робити паралельно з Epic 3. S1-020 (prompt) теж.
- **Порядок імплементації:**
  1. S1-019 (Pydantic-моделі) — блокує все інше
  2. S1-020 (Prompt) — залежить від S1-019, блокує S1-021
  3. S1-021 (ArchitectAgent) — залежить від S1-019 + S1-020
  4. S1-022 (Persistence) — залежить від S1-019, можна паралельно з S1-020/S1-021
- **Блокує:** Epic 5 (API endpoint `POST /courses` оркеструє Ingestion → ArchitectAgent → Save)

## Задачі

| ID | Назва | Статус | Тести | Примітка |
|:---|:---|:---|:---|:---|
| S1-019 | Pydantic-моделі output | | ~10 | [spec](T1-course-structure/T019-course-structure.md) · [issue](T1-course-structure/T019-github-issue.md) — 7 моделей, validation |
| S1-020 | System prompt v1 | | ~8 | [spec](T2-architect-prompt/T020-architect-prompt.md) · [issue](T2-architect-prompt/T020-github-issue.md) — YAML + loader |
| S1-021 | ArchitectAgent клас | | ~10 | [spec](T3-architect-agent/T021-architect-agent.md) · [issue](T3-architect-agent/T021-github-issue.md) — orchestration via ModelRouter |
| S1-022 | Збереження структури курсу | | ~12 | [spec](T4-save-course/T022-save-course.md) · [issue](T4-save-course/T022-github-issue.md) — Pydantic → ORM mapping |

**Загалом: 2 дні, ~40 тестів**

## Ризики

- **Structured output quality** — LLM може генерувати невалідний JSON або неповну структуру. Мітигація: Pydantic validation + retry + fallback на іншу модель.
- **Prompt iteration** — перший промпт рідко ідеальний. Версіонування в YAML дозволяє A/B testing. Еталонна розбивка (S1-030) допоможе оцінити якість.
- **Context window** — великий курс (4+ годин відео + 100+ слайдів) може перевищити контекст. Gemini з 1M context — primary, при overflow — chunked processing.

---

## Прийняті архітектурні рішення

### 1. Output суфікс для Pydantic-моделей

`ExerciseOutput`, `ConceptOutput`, `LessonOutput`, `ModuleOutput` — суфікс `Output` для уникнення name collision з ORM класами (`Exercise`, `Concept`, `Lesson`, `Module` в `storage/orm.py`). `CourseStructure` та `SlideRange` не конфліктують.

### 2. Replace-стратегія для persistence

При повторному генеруванні `CourseStructureRepository.save()` видаляє існуючі модулі (cascade delete → lessons → concepts → exercises) і створює нові. Простіше ніж diff/merge і достатньо для MVP.

### 3. flush() замість commit()

Як і `SourceMaterialRepository` — caller контролює transaction boundary. При помилці — rollback відміняє все.

### 4. Prompt versioning у YAML

`prompts/architect/v1.yaml` з полем `version`. Для A/B тестування — `v2.yaml`, `v3.yaml`. Prompt loader відповідає за завантаження, ArchitectAgent приймає `prompt_path` як параметр.

### 5. Empty lists → None для JSONB

ORM JSONB fields nullable. Pydantic default — empty list. При persistence: empty `[]` → `None` в DB для consistency.

---

## Shared Pydantic Models (S1-019)

Ці моделі використовуються всіма задачами Epic 4:

```python
# models/course.py (NEW — додаються до існуючого)
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
    lessons: list[LessonOutput] = Field(default_factory=list)

class CourseStructure(BaseModel):
    title: str
    description: str = ""
    modules: list[ModuleOutput] = Field(default_factory=list)
```

---

## Task Specs

Детальні специфікації для кожної задачі:

```
E4-architect-agent/
├── T1-course-structure/     S1-019: Pydantic output models
├── T2-architect-prompt/     S1-020: System prompt v1 + loader
├── T3-architect-agent/      S1-021: ArchitectAgent class
└── T4-save-course/          S1-022: CourseStructureRepository
```

Кожна папка містить `T0XX-*.md` (повна spec з кодом і тестами) + `T0XX-github-issue.md` (summary).
