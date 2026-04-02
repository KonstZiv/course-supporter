# VD-008: Factory + Config

**Фаза:** 5 — Integration
**Пріоритет:** Середній
**Залежності:** VD-006, CP-5

## Що робимо

Wire VDPipeline в існуючу інфраструктуру: factory, config, external_services.yaml.

## Яким чином

### Factory (ingestion/factory.py)
- create_vd_pipeline() → VDPipeline з FrameSampler, VisualAnalyzer, MemoryPipeline
- create_video_processor() → VideoProcessor з stt_router, vd_pipeline, aligner

### Config (config.py)
- VD_GEMINI_KEYS — key pool (comma-separated)
- VD_MODEL — model name (default: gemini-3.1-flash-lite-preview)
- VD_RPM_LIMIT — rate limit (default: 15)

### external_services.yaml
- Один новий action: `visual_eyes` з chain [gemini-3.1-flash-lite-preview]

## Acceptance criteria

- [ ] VDPipeline створюється через factory
- [ ] Config env vars працюють
- [ ] external_services.yaml має visual_eyes action
