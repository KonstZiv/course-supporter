# VD-010: Audio Chunking

**Статус:** TODO
**Фаза:** 5 — Integration
**Пріоритет:** High
**Залежності:** VD-009
**Блокує:** —

Для 2+ год відео: MP3 > 25MB перевищує OpenAI STT ліміт. `chunk_audio_if_needed()` — FFmpeg split на segments + merge STT results з timestamp offset correction. ElevenLabs/Deepgram не потребують chunking.
