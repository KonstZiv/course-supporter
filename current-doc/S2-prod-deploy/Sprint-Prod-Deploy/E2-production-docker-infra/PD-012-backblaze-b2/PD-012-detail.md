# PD-012: Backblaze B2 Integration — Detail

## Контекст

Існуючий `S3Client` використовує aiobotocore — S3-compatible API. Backblaze B2 підтримує S3-compatible API. Очікуємо мінімальні зміни.

## Backblaze B2 Setup

### 1. Створити bucket в B2 Console

```
Bucket name: course-supporter-materials
Type: Private
```

### 2. Створити Application Key

```
Key name: course-supporter-api
Bucket: course-supporter-materials (scoped)
Capabilities: readFiles, writeFiles, listFiles, deleteFiles
```

Результат: `keyID` (= S3_ACCESS_KEY) та `applicationKey` (= S3_SECRET_KEY).

### 3. Environment Variables

```bash
S3_ENDPOINT=https://s3.us-west-004.backblazeb2.com  # varies by region
S3_ACCESS_KEY=<keyID>
S3_SECRET_KEY=<applicationKey>
S3_BUCKET=course-supporter-materials
```

## Зміни в коді

### S3Client — ensure_bucket()

B2 не підтримує `create_bucket` через S3 API (тільки через B2 native API). Потрібно зробити `ensure_bucket()` graceful:

```python
async def ensure_bucket(self) -> None:
    """Ensure bucket exists. On B2, bucket must be pre-created via console."""
    try:
        await self._client.head_bucket(Bucket=self._bucket)
        logger.info("s3_bucket_verified", bucket=self._bucket)
    except self._client.exceptions.ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "404":
            logger.warning(
                "s3_bucket_not_found",
                bucket=self._bucket,
                hint="Create bucket manually in B2 console",
            )
            raise
        raise
```

### Config — region-aware endpoint

Можливо потрібен `S3_REGION` env var:

```python
# config.py
s3_region: str = "us-west-004"
```

## Тестування

```bash
# Тест підключення (з production credentials):
python -c "
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
        # Upload test file
        await s3.upload(b'hello', 'test/hello.txt', 'text/plain')
        # Download
        data = await s3.download('test/hello.txt')
        assert data == b'hello'
        print('B2 integration OK')

asyncio.run(test())
"
```

## Definition of Done

- [ ] B2 bucket створено
- [ ] Application key створено (scoped)
- [ ] `S3Client` працює з B2
- [ ] `ensure_bucket()` graceful для B2
- [ ] Upload/download тест проходить
- [ ] `.env.prod.example` оновлений
- [ ] Документ оновлений відповідно до фінальної реалізації
