# PD-013: Streaming Upload (1GB)

## Що

Chunked upload великих файлів (до 1GB) напряму в S3 через multipart upload. Без тримання файлу в RAM.

## Навіщо

Стандартний upload читає весь файл в пам'ять. 1GB відео з 2 uvicorn workers = потенційний OOM на 2GB RAM VPS. Streaming вирішує це — ~10-20 MB RAM per upload незалежно від розміру.

## Ключові рішення

- S3 Multipart Upload: create → upload parts (10MB chunks) → complete
- FastAPI `UploadFile` async read chunk by chunk
- nginx `proxy_request_buffering off` — stream through

## Acceptance Criteria

- [ ] `S3Client.upload_stream()` з multipart upload
- [ ] Upload endpoint використовує streaming
- [ ] Upload 100MB+ не підвищує RAM більш ніж на 20MB
- [ ] Тести на multipart upload logic
