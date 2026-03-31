# VD-005: OCR Extractor (умовний)

**Статус:** TODO
**Фаза:** 4 — Implementation
**Пріоритет:** Medium
**Залежності:** VD-001, VD-SPIKE-C
**Блокує:** VD-007

УМОВНИЙ — тільки якщо Spike B показав Vision LLM code accuracy < 90%. OCR engine (Surya/PaddleOCR/Tesseract з Spike C) + LLM post-processing (DeepSeek/GPT-4o Mini, action `ocr_correction`). Pre-processing для dark IDE themes.
