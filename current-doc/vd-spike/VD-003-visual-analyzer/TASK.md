# VD-003: Visual Analyzer — Eyes (visual_analyzer.py)

**Фаза:** 4 — Implementation
**Пріоритет:** Високий
**Залежності:** VD-001, CP-1 passed

## Що робимо

Port Eyes step з `scripts/spike_vd_multimodel.py`. Single-pass Vision LLM з prompt v3 (Markdown output).

**УВАГА:** Оригінальний план мав two-pass (classify → detail). Spike показав що single-pass + hierarchical memory краще. Цей модуль реалізує ТІЛЬКИ Eyes — per-frame Vision LLM analysis.

## Яким чином

Створити `src/course_supporter/vd/visual_analyzer.py`:

### VisualAnalyzer class

- model: gemini-3.1-flash-lite-preview (єдина модель, не chain)
- rpm_limit: 15
- context_max_gap_sec: 7.0
- max_context_images: 2

### Per-frame pipeline:
1. Build context: previous frames (within 7s, same scene, max 2)
2. Build prompt: course context + scene context + prompt v3
3. Call Vision LLM with 1-3 images
4. Parse Markdown response → EyesResult
5. Rate limiting via key pool / semaphore

### Prompt v3
Port з spike. Зберегти в `src/course_supporter/vd/prompts/eyes_v3.txt`.
- Language-agnostic Markdown output
- Scene Composition + Elements with type-specific fields
- code_area, text_area, ui_element, image_or_diagram

### ModelRouter vs direct SDK
Рекомендація: ModelRouter з єдиним provider — для logging і можливості fallback у майбутньому.

## Acceptance criteria

- [ ] 10 GT frames → accuracy ≥95% (виправлений GT)
- [ ] Rate limiting працює (≤15 RPM)
- [ ] Resumable: перервано → продовжує з місця зупинки
- [ ] Prompt v3 ідентичний spike version

## ⚠️ Після завершення — виконати CP-2: Перевірка Visual Analyzer

Див. VD-IMPLEMENTATION-PLAN.md, секція CP-2. Людина перевіряє 10 кадрів: повнота витягування, точність коду, hallucinations. ~1 година.
