# VD-004: Memory Pipeline (memory_pipeline.py)

**Фаза:** 4 — Implementation
**Пріоритет:** Високий
**Залежності:** VD-001, VD-003, CP-2 passed

## Що робимо

Port hierarchical memory з spike. Три рівні aggregation VD-потоку (instant → scene → course).

**УВАГА:** Оригінальний план мав простий aggregation (merge by timestamps). Spike показав що hierarchical memory дає значно кращі результати (85% → 99% merged accuracy).

## Яким чином

Створити `src/course_supporter/vd/memory_pipeline.py`:

### MemoryPipeline class

**Instant merge** (per-scene, code-based):
- Збирає Eyes responses з усіх кадрів сцени
- Code-based overlap detection (без LLM)
- Fallback на LLM merge якщо overlap detection не спрацював
- Результат: InstantMemory з merged_text

**Scene synthesis** (per-scene, LLM):
- Input: Eyes responses + merged_text
- LLM call → scene_type, summary (2-3 sentences Ukrainian), topics, importance
- Prompt: `src/course_supporter/vd/prompts/scene_memory.txt`
- Результат: SceneMemory

**Course memory** (running, LLM):
- Input: previous course memory + new SceneMemory
- LLM call → updated context ≤200 words
- Prompt: `src/course_supporter/vd/prompts/course_memory.txt`
- Результат: CourseMemory

### КРИТИЧНИЙ РИЗИК: Merge corruption

Під час spike 2.5-flash модель ЗМІНИЛА код при merge: `+` → `*`, `0.937` → `0.837`. Хоча ми використовуємо lite (де це не виявлено), потрібно:
- Code-based merge WITHOUT LLM як primary
- LLM merge тільки як fallback
- Verify merged text проти originals

## Acceptance criteria

- [ ] golden_075 merge recovers occluded code (100% merged accuracy)
- [ ] Course memory ≤200 слів після 50+ scenes
- [ ] Scene synthesis: правильний scene_type для slide/code/terminal
- [ ] 0 merge corruption на тестових даних

## ⚠️ Після завершення — виконати CP-3: Перевірка Memory Pipeline

Див. VD-IMPLEMENTATION-PLAN.md, секція CP-3. Людина перевіряє 5 scenes: merge corruption, scene types, course memory drift. ~30 хвилин.
