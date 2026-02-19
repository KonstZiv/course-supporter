# S2-032: Extract whisper transcription

**Epic:** EPIC-4 — Heavy Steps Extraction
**Оцінка:** 3h

---

## Мета

Whisper transcription — окрема injectable function

## Що робимо

Виділити whisper виклик з VideoProcessor в окрему функцію

## Як робимо

1. Створити local_transcribe(audio_url, params) → Transcript
2. Функція: download audio → whisper → structured result
3. Нічого про DB, S3 storage, ORM

## Очікуваний результат

local_transcribe працює автономно, можна замінити на lambda_transcribe

## Як тестуємо

**Автоматизовано:** Unit test з mock whisper: перевірити input/output contract

**Human control:** Запустити ingestion відео → результат ідентичний до рефакторингу

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
