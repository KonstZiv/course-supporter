# S1-019: Pydantic Output Models (CourseStructure)

## Мета

Визначити Pydantic-моделі для structured output ArchitectAgent: `CourseStructure` → `ModuleOutput` → `LessonOutput` → `ConceptOutput` + `ExerciseOutput`. Використовуються як `response_schema` для LLM і як проміжний формат для persistence.

## Що робимо

1. **7 нових Pydantic-моделей** у `models/course.py`: `SlideRange`, `WebReference`, `ExerciseOutput`, `ConceptOutput`, `LessonOutput`, `ModuleOutput`, `CourseStructure`
2. **Оновити exports** у `models/__init__.py`
3. **~10 unit-тестів**: serialization round-trip, defaults, validation (difficulty 1–5), full hierarchy

## Очікуваний результат

- `CourseStructure` серіалізується в JSON і десеріалізується без втрат
- `difficulty_level` валідується (1–5)
- Full hierarchy: Course → Module → Lesson → Concept + Exercise
- `make check` проходить

## Контрольні точки

- [ ] 7 нових Pydantic-моделей визначені
- [ ] Суфікс `Output` для уникнення collision з ORM
- [ ] `difficulty_level` ge=1, le=5
- [ ] Serialization round-trip працює
- [ ] Exports оновлені
- [ ] ~10 тестів зелені
- [ ] `make check` проходить

## Деталі

Повний spec: **T019-course-structure.md**

## Блокує

- S1-020 (System prompt — schema описана в промпті)
- S1-021 (ArchitectAgent — повертає CourseStructure)
- S1-022 (Persistence — Pydantic → ORM mapping)
