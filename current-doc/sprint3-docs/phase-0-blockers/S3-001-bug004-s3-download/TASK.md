# S3-001: BUG-004 — S3 download fails for B2 materials

**Phase:** 0 (Production Blockers)
**Складність:** S
**Статус:** IN PROGRESS (fix written, not committed)

## Проблема

`s3.download_file()` не працює для матеріалів збережених в Backblaze B2. Worker отримує помилку при спробі завантажити файл для обробки.

**Симптом:** Ingestion jobs для B2-матеріалів (presentations, texts) зависають або падають з помилкою.

## Кореневі причини (гіпотеза)

1. **Відсутній BotoConfig** — `S3Client.open()` створює aiobotocore client без `signature_version="s3v4"` і `addressing_style="path"`. B2 вимагає SigV4 + path-style addressing.
2. **Недостатня діагностика** — `download_file()` не ловить `ClientError` для детального логування.

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `src/course_supporter/storage/s3.py` | Додати `BotoConfig` з SigV4 + path-style в `open()`. Додати `ClientError` handling з логуванням в `download_file()` |
| `tests/unit/test_s3_client.py` | Нові тести: ClientError re-raise, temp cleanup on stream error, config verification |

## Деталі реалізації

### 1. Виправити `open()` (s3.py:50-64)

```python
from botocore.config import Config as BotoConfig

# В open():
config=BotoConfig(
    signature_version="s3v4",
    s3={"addressing_style": "path"},
),
```

### 2. Покращити error handling в `download_file()` (s3.py:282-352)

Додати `try/except ClientError` навколо `get_object()` з детальним логуванням (error_code, key, bucket).

### 3. Нові тести

- `test_download_file_raises_client_error` — перевірка що ClientError перекидається
- `test_download_file_cleans_temp_on_stream_error` — temp file видаляється при помилці streaming
- `test_open_uses_s3v4_signature` — перевірка конфігурації

## Поточний стан

Виправлення написані в попередній сесії але **НЕ закомічені**. Потрібно:
1. Перевірити що зміни ще в робочому дереві
2. Запустити тести (`uv run pytest tests/unit/test_s3_client.py`)
3. Перевірити на production (deploy + retry failed jobs)

## Acceptance Criteria

- [ ] `S3Client.open()` створює client з `BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"})`
- [ ] `download_file()` логує `ClientError` з error_code перед перекиданням
- [ ] 3 нові тести проходять
- [ ] Всі 18 тестів в `test_s3_client.py` проходять
- [ ] На production: B2 матеріали обробляються (потребує deploy + retry)
