# Phase 6: StructureNode (XL)

**Складність:** XL (Extra Large)
**Залежності:** Phase 5 (послідовно після Course + SourceMaterial removal)
**Задачі:** S3-015
**PR:** 1 великий PR
**Risk:** HIGH

## Мета

Замінити 4-рівневу фіксовану ієрархію (Module → Lesson → Concept → Exercise) на рекурсивний StructureNode з `node_type` StrEnum. Дозволяє довільну глибину генерованої структури.

## Ключове рішення

**LLM output format НЕ змінюється.** Зберігаємо Module/Lesson/Concept/Exercise в промпті та Pydantic output models. Конвертація в StructureNode на рівні persistence (в `arq_generate_structure` task).

## Нова таблиця (25+ полів, 6 секцій)

1. **System** — id, snapshot FK, parent FK (self-ref), node_type, order, timestamps
2. **Formal** — title, description, learning_goal, expected_knowledge/skills, prerequisites, difficulty, estimated_duration
3. **Results** — success_criteria, assessment_method, competencies
4. **Methodological** — key_concepts, common_mistakes, teaching_strategy, activities
5. **Context** — teaching_style, deep_dive_references, content_version
6. **Material refs** — timecodes, slide_references, web_references
7. **Semantic** — embedding Vector(1536)

Повна специфікація: `current-doc/backlog.md` → "Replace Module/Lesson/Concept/Exercise with recursive StructureNode"

## Критерії завершення

- [ ] StructureNode таблиця створена з всіма полями
- [ ] Module, Lesson, Concept, Exercise таблиці видалені
- [ ] Generation pipeline конвертує LLM output → StructureNode tree
- [ ] API повертає recursive structure
- [ ] Data migration з 4 таблиць
