# S1-012: VideoProcessor (Primary — Gemini Vision)

## Мета

Реалізувати основний відеопроцесор через Gemini Vision API: завантаження відео → транскрипція з таймкодами → `SourceDocument`.

## Що робимо

1. **GeminiVideoProcessor** — upload відео через Gemini File API, отримати транскрипцію з таймкодами
2. **VideoProcessor** shell — composition pattern, делегує до GeminiVideoProcessor, shell для fallback (S1-013)
3. **Prompt** — системний промпт для video_analysis action
4. **Unit-тести** — ~8 тестів з мокнутим router/Gemini SDK

## Контрольні точки

- [ ] `GeminiVideoProcessor.process()` повертає `SourceDocument` з transcript chunks
- [ ] Таймкоди в metadata (`start_sec`, `end_sec`)
- [ ] Валідація `source_type == "video"` — інакше `UnsupportedFormatError`
- [ ] `router=None` → `ProcessingError`
- [ ] `VideoProcessor` делегує до `GeminiVideoProcessor`
- [ ] Fallback shell готовий для S1-013
- [ ] `make check` проходить

## Залежності

- **Блокується:** S1-011 (schemas + ABC)
- **Блокує:** S1-013 (whisper fallback)
