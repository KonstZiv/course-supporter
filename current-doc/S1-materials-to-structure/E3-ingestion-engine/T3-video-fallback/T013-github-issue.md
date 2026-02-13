# S1-013: VideoProcessor (Fallback — FFmpeg + Whisper)

## Мета

Додати fallback для відеообробки: FFmpeg витягує аудіо → OpenAI Whisper транскрибує → `SourceDocument`. Інтегрувати з `VideoProcessor` (Gemini fails → Whisper).

## Що робимо

1. **WhisperVideoProcessor** — FFmpeg subprocess для audio extraction + Whisper для транскрипції
2. **VideoProcessor update** — підключити WhisperVideoProcessor як fallback
3. **System dependency** — FFmpeg (не Python-пакет), mock у тестах
4. **Unit-тести** — ~7 тестів з мокнутим FFmpeg + Whisper

## Контрольні точки

- [ ] `WhisperVideoProcessor.process()` повертає `SourceDocument` з transcript chunks
- [ ] Таймкоди сегментів у metadata (`start_sec`, `end_sec`)
- [ ] FFmpeg not found → `ProcessingError`
- [ ] FFmpeg subprocess error → `ProcessingError`
- [ ] `VideoProcessor`: Gemini fails → Whisper fallback
- [ ] Обидва fail → raise останню помилку
- [ ] `make check` проходить

## Залежності

- **Блокується:** S1-011, S1-012
- **Блокує:** немає
