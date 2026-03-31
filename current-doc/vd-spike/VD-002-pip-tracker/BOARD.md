# VD-002: PiP Tracker

**Статус:** TODO
**Фаза:** 4 — Implementation
**Пріоритет:** High
**Залежності:** VD-001
**Блокує:** VD-003

3-рівневий PiP tracking: Vision LLM initial detection → temporal diff (cv2.absdiff + zone motion) → periodic validation. Обробка no-PiP випадку. PiPEvent logging.
