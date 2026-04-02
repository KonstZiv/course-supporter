# VD-005: Pipeline Orchestrator (pipeline.py)

**Фаза:** 4 — Implementation
**Пріоритет:** Високий
**Залежності:** VD-002, VD-003, VD-004, CP-3 passed

## Що робимо

VDPipeline — orchestrator що з'єднує FrameSampler → VisualAnalyzer → MemoryPipeline. Self-contained: нічого не знає про STT.

## Яким чином

Створити `src/course_supporter/vd/pipeline.py`:

### VDPipeline class

```python
class VDPipeline:
    def __init__(self, sampler, analyzer, memory): ...
    async def process(self, video_path: Path) -> VDResult: ...
```

### Pipeline:
1. Create temp dir for frames
2. sampler.extract(video_path, temp_dir) → FrameSamplingResult
3. For each scene:
   - analyzer.analyze_scene(scene, frames, temp_dir, course_context)
   - memory.process_scene(eyes_results, scene, course_memory)
4. Build VDResult
5. Cleanup temp dir (finally block)

### Temp cleanup
`shutil.rmtree(temp_dir, ignore_errors=True)` в finally block.

## Acceptance criteria

- [ ] E2E на 10-хв відео: video → VDResult
- [ ] Temp cleanup працює (навіть при exceptions)
- [ ] VDResult contains all scenes with memories

## ⚠️⛔ ПІСЛЯ ЗАВЕРШЕННЯ — виконати CP-4: Gate Review (ОБОВ'ЯЗКОВО)

Див. VD-IMPLEMENTATION-PLAN.md, секція CP-4. Це ГОЛОВНИЙ checkpoint.
Людина перевіряє 2 повних відео: coverage, code accuracy, timeline, performance. ~2 години.
**НЕ ПЕРЕХОДИТИ ДО ФАЗИ 5 без CP-4 pass.**
