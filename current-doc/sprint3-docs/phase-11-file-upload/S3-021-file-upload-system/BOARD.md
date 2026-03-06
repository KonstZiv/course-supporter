# S3-021: Full File Upload System

**Тип:** New feature
**Пріоритет:** High
**Складність:** L
**Phase:** 11

## Опис

Presigned URL upload flow (клієнт → S3 напряму), storage management (file list, usage), S3 cleanup при видаленні сутностей, multi-tenant isolation через S3 key prefix.

## Вплив

- S3 client — 4 нових методи (presigned URLs, listing, usage, delete)
- Нові API endpoints — upload-url, confirm-upload, storage management
- Існуючі endpoints — S3 cleanup при DELETE materials/nodes
- Security — tenant isolation через key prefix + private bucket

## Definition of Done

- Presigned URL upload працює з B2/MinIO
- Файли видно в storage listing з usage tracking
- Видалення файлів/вузлів каскадно чистить S3
- Tenant isolation забезпечена
- CORS deployment задокументовано
- Всі endpoints покриті тестами
