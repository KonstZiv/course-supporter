# PD-012: Backblaze B2 Integration — Detail

## Контекст

Існуючий `S3Client` використовує aiobotocore — S3-compatible API. Backblaze B2 підтримує S3-compatible API. Мінімальні зміни в коді.

## Зміни в коді

- `S3Client.ensure_bucket()` — тепер тільки перевіряє існування bucket (`head_bucket`), не намагається створити. Якщо bucket відсутній — логує помилку та прокидує exception.
- Тест `test_ensure_bucket_creates_if_missing` → `test_ensure_bucket_raises_if_missing`
- `make check` — 385 тестів зелені

## Завдання при deploy (ручні кроки)

### Крок 1: Створити bucket в B2 Console

Зайти на https://secure.backblaze.com/b2_buckets.htm

```
Bucket name: course-supporter-materials
Type: Private
Default Encryption: Enable (SSE-B2)
Object Lock: Disabled
```

### Крок 2: Створити Application Key

B2 Console → App Keys → Add a New Application Key

```
Key name: course-supporter-api
Bucket: course-supporter-materials (scoped до одного bucket!)
Capabilities: readFiles, writeFiles, listFiles, deleteFiles
```

Результат:
- `keyID` → використати як `S3_ACCESS_KEY`
- `applicationKey` → використати як `S3_SECRET_KEY` (показується ОДИН раз, зберегти!)

### Крок 3: Визначити S3 endpoint

Endpoint залежить від регіону bucket. Побачити можна в B2 Console → Buckets → Endpoint.

Формат: `https://s3.<region>.backblazeb2.com`

Приклад: `https://s3.us-west-004.backblazeb2.com`

### Крок 4: Додати змінні в `.env.prod` на VPS

```bash
S3_ENDPOINT=https://s3.<region>.backblazeb2.com
S3_ACCESS_KEY=<keyID>
S3_SECRET_KEY=<applicationKey>
S3_BUCKET=course-supporter-materials
```

### Крок 5: Верифікація після запуску app

З контейнера course-supporter-app:

```bash
docker compose -f docker-compose.prod.yaml exec app python -c "
import asyncio
from course_supporter.storage.s3 import S3Client
from course_supporter.config import settings

async def test():
    s3 = S3Client(
        endpoint_url=settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key.get_secret_value(),
        bucket=settings.s3_bucket,
    )
    async with s3:
        await s3.ensure_bucket()
        await s3.upload_file('test/hello.txt', b'hello', 'text/plain')
        print('B2 integration OK')

asyncio.run(test())
"
```

Після верифікації — видалити тестовий файл в B2 Console або залишити.

## Definition of Done

- [ ] B2 bucket створено (ручна дія в B2 Console)
- [ ] Application key створено (scoped, ручна дія в B2 Console)
- [x] `S3Client` працює з B2 (S3-compatible API, aiobotocore)
- [x] `ensure_bucket()` graceful для B2 (verify-only, no create_bucket)
- [ ] Upload/download тест проходить (верифікація після налаштування B2)
- [x] `.env.prod.example` оновлений (B2 змінні вже присутні)
- [x] `make check` зелений (385 тестів)
- [x] Документ оновлений відповідно до фінальної реалізації
