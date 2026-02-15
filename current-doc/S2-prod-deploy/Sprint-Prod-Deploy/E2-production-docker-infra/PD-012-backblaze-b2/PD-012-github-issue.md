# PD-012: Backblaze B2 Integration

## Що

Підключити Backblaze B2 як S3 storage замість MinIO. Перевірити сумісність існуючого `S3Client` з B2 S3-compatible API.

## Навіщо

MinIO — для local dev. Production потребує managed object storage. Backblaze B2 — S3-compatible, 10 GB free tier.

## Ключові рішення

- `S3Client` (aiobotocore) працює з B2 без змін коду — тільки env vars
- B2 S3 endpoint format: `https://s3.<region>.backblazeb2.com`
- Application key з обмеженим scope (один bucket)

## Acceptance Criteria

- [ ] `S3Client` підключається до B2
- [ ] Upload/download файлів працює
- [ ] `ensure_bucket()` працює або gracefully пропускається (B2 bucket створений через UI)
- [ ] `.env.prod.example` оновлений з B2 змінними
