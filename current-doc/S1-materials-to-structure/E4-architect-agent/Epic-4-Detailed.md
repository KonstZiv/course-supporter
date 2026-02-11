# Epic 4: Architect Agent (Методист)

## Мета

AI-агент, що аналізує `CourseContext` (результат Ingestion) і генерує структуровану навчальну програму курсу. Після цього епіку — система перетворює сирі матеріали на повний навчальний план: модулі → уроки → концепції з cross-references на таймкоди/слайди/URL + практичні завдання.

## Що робимо

Чотири компоненти:

1. **Pydantic-моделі output** (S1-019) — `CourseStructure`, `Module`, `Lesson`, `Concept`, `Exercise`, `SlideRange`, `WebReference`. Повний набір типізованих схем для structured output агента. Ці моделі використовуються і як response schema для LLM, і як API response.
2. **System prompt v1** (S1-020) — промпт для Architect Agent в `prompts/architect/v1.yaml`. Інструкції: як аналізувати CourseContext, яку ієрархію генерувати, як створювати concept cards із cross-references. Версіонується для A/B тестування.
3. **ArchitectAgent клас** (S1-021) — `async def run(context: CourseContext) -> CourseStructure`. Виклик LLM через ModelRouter (action="course_structuring"), валідація output через Pydantic, retry при невалідному JSON або неповній структурі.
4. **Збереження структури** (S1-022) — маппінг `CourseStructure` (Pydantic) → ORM-моделі (modules, lessons, concepts, exercises). Транзакційне збереження з FK constraints, replace-стратегія при повторному генеруванні.

## Для чого

Architect Agent — ядро бізнес-логіки проєкту. Саме він перетворює "купу матеріалів" на "структурований курс". Якість його output визначає цінність усього продукту. Prompt engineering тут — ключова робота.

## Контрольні точки

- [ ] Pydantic-моделі: `CourseStructure` серіалізується/десеріалізується без втрат
- [ ] System prompt: чітко описує що, як і в якому форматі генерувати
- [ ] ArchitectAgent: приймає CourseContext → повертає валідну CourseStructure через ModelRouter
- [ ] Structured output: LLM response валідується Pydantic, при невалідному — retry/fallback
- [ ] Persistence: CourseStructure → DB зберігається транзакційно (all-or-nothing)
- [ ] Re-generation: повторний виклик замінює попередню структуру (не дублює)
- [ ] `make check` проходить

## Залежності

- **Блокується:** Epic 2 (ModelRouter), Epic 3 (CourseContext з Ingestion)
- **Частковий паралелізм:** S1-019 (Pydantic-моделі) не залежить від Ingestion — можна робити паралельно з Epic 3. S1-020 (prompt) теж.
- **Блокує:** Epic 5 (API endpoint `POST /courses` оркеструє Ingestion → ArchitectAgent → Save)

## Задачі

| ID | Назва | Естімейт | Примітка |
|:---|:---|:---|:---|
| S1-019 | Pydantic-моделі output | 0.5 дня | Схеми = контракт між LLM і API |
| S1-020 | System prompt v1 | 0.5 дня | Prompt engineering, ітеративний процес |
| S1-021 | ArchitectAgent клас | 0.5 дня | Orchestration через ModelRouter |
| S1-022 | Збереження структури курсу | 0.5 дня | Pydantic → ORM mapping, transactions |

**Загалом: 2 дні**

## Ризики

- **Structured output quality** — LLM може генерувати невалідний JSON або неповну структуру. Мітигація: Pydantic validation + retry + fallback на іншу модель.
- **Prompt iteration** — перший промпт рідко ідеальний. Версіонування в YAML дозволяє A/B testing. Еталонна розбивка (S1-030) допоможе оцінити якість.
- **Context window** — великий курс (4+ годин відео + 100+ слайдів) може перевищити контекст. Gemini з 1M context — primary, при overflow — chunked processing.
