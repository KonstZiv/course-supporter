# S1-014: PresentationProcessor (PDF + PPTX)

## Мета

Обробка презентацій (PDF через PyMuPDF, PPTX через python-pptx): витягування тексту зі слайдів + опційний аналіз зображень через Vision LLM.

## Що робимо

1. **PresentationProcessor** — визначення формату за розширенням (.pdf / .pptx)
2. **PDF path** — `fitz.open()` → text extraction + optional pixmap → Vision LLM
3. **PPTX path** — `Presentation()` → text із shapes + optional diagrams → Vision LLM
4. **Vision LLM** — `router.complete(action="presentation_analysis")` для slide_description chunks
5. **Graceful degradation** — LLM fails → тільки text chunks, без crash
6. **Unit-тести** — ~10 тестів

## Контрольні точки

- [ ] PDF → `ContentChunk(chunk_type=SLIDE_TEXT)` з правильною нумерацією
- [ ] PPTX → `ContentChunk(chunk_type=SLIDE_TEXT)` з text із shapes
- [ ] Vision LLM → `ContentChunk(chunk_type=SLIDE_DESCRIPTION)`
- [ ] Без router → тільки text extraction
- [ ] Невідоме розширення → `UnsupportedFormatError`
- [ ] LLM failure → graceful fallback до text-only
- [ ] `make check` проходить

## Залежності

- **Блокується:** S1-011
- **Блокує:** немає
