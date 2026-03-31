# VD-009: Redesign VideoProcessor

**Статус:** TODO
**Фаза:** 5 — Integration
**Пріоритет:** Critical
**Залежності:** VD-007
**Блокує:** VD-014

Замінити GeminiVideoProcessor/WhisperVideoProcessor на новий VideoProcessor. Паралельний STT + VD: asyncio.create_task(stt) + frame extraction, Visual analysis з STT context. SourceDocument з metadata: strategy, stt_provider, vd_frames_total, pip_events.
