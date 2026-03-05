# S3-006: Platform Allowlist for Web Sources

**Phase:** 1 (Cleanup)
**Складність:** S
**Статус:** PENDING

## Контекст

`source_type=web` приймає будь-який URL, але не всі платформи можна обробити. VideoProcessor використовує yt-dlp, WebProcessor — trafilatura. Деякі платформи блокують scraping, вимагають auth, або serve JS-only content.

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `config/platforms.yaml` (або в existing config) | НОВИЙ — allowlist по domain → source_type |
| `src/course_supporter/config.py` | Pydantic model для platform config |
| `src/course_supporter/api/upload_validation.py` | Warning для непідтверджених платформ |
| `src/course_supporter/ingestion/` | Можливо processor-level validation |
| `tests/` | Тести для allowlist validation |

## Деталі реалізації

### 1. Config file

```yaml
platforms:
  video:
    verified:
      - youtube.com
      - youtu.be
      - vimeo.com
    notes:
      youtube.com: "Public videos only. Age-restricted may fail."
  web:
    verified:
      - wikipedia.org
      - github.com
      - docs.python.org
    notes:
      medium.com: "Limited access, may hit paywall"
```

### 2. Validation behavior

**Warn, not block:** коли URL domain не в allowlist:
- API response включає warning: "This platform has not been verified, processing may fail."
- Ingestion все одно запускається (best effort)

### 3. Testing

Протестувати major платформи:
- **Video:** YouTube, Vimeo, Dailymotion
- **Web:** Wikipedia, GitHub, dev blogs, Medium

## Acceptance Criteria

- [ ] Platform allowlist визначений в config
- [ ] Непідтверджені URLs отримують warning (не блокуються)
- [ ] Verified platforms задокументовані
- [ ] Тести покривають warning logic
