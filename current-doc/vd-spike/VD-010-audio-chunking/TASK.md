# VD-010: Audio Chunking

**Фаза:** 5 — Integration
**Пріоритет:** High
**Залежності:** VD-009

## Що робимо

Додаємо підтримку audio chunking для довгих відео (2+ години). OpenAI STT API має ліміт 25MB на файл — MP3 з 2-годинного відео перевищує цей ліміт. ElevenLabs (1GB) та Deepgram (2GB) не потребують chunking.

## Яким чином

1. **Функція `chunk_audio_if_needed()`:**
   - Перевірити розмір MP3 файлу
   - Якщо > 25MB і поточний STT provider = OpenAI → розбити на segments
   - Для ElevenLabs та Deepgram — повернути оригінальний файл as-is

2. **FFmpeg split:**
   - Розбити MP3 на segments по ~20 хвилин (з невеликим overlap для continuity)
   - Використовувати FFmpeg `-ss` та `-t` для точного розрізу
   - Зберегти segments у тимчасовій директорії

3. **Merge STT results:**
   - Транскрибувати кожен segment окремо
   - Об'єднати результати з корекцією timestamp offset
   - Segment 2 timestamps += segment_1_duration
   - Обробити overlap зону: видалити дублікати слів на стику

4. **Integration з VideoProcessor:**
   - Викликати `chunk_audio_if_needed()` перед передачею до `STTRouter`
   - Якщо chunking відбувся — merge results після транскрипції

## Результат

- Функція `chunk_audio_if_needed()` у відповідному модулі
- FFmpeg split на segments з overlap
- Merge з timestamp offset correction
- Тільки для OpenAI fallback (ElevenLabs/Deepgram не потребують)

## Як перевіряємо

```bash
uv run mypy src/course_supporter/ingestion/video.py    # strict, no errors
uv run ruff check src/course_supporter/ingestion/       # no lint errors
# Manual: взяти 2+ годинне відео, перевірити що OpenAI STT працює через chunking
# Перевірити що timestamps на стику segments коректні (немає зсуву/дублювання)
```
