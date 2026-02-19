# S2-009: Ingestion completion callback

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 3h

---

## Мета

Після завершення ingestion: оновити entry, інвалідувати fingerprints, trigger revalidation маппінгів

## Що робимо

Callback функція що викликається worker-ом після завершення ingestion job

## Як робимо

1. on_ingestion_complete(material_entry_id, success, result):
   - Оновити MaterialEntry (processed_content або error_message)
   - Очистити pending_job_id/pending_since
   - Інвалідувати content_fingerprint → cascade _invalidate_up
   - Знайти SlideVideoMappings де цей матеріал є blocking factor → revalidate
2. Інтегрувати callback в ARQ task wrapper

## Очікуваний результат

Завершення ingestion каскадно оновлює entry, fingerprints і pending маппінги

## Як тестуємо

**Автоматизовано:** Integration test: ingestion complete → entry updated + fingerprints invalidated + mappings revalidated

**Human control:** Завантажити матеріал з маппінгом в pending_validation → дочекатись ingestion → перевірити що маппінг став validated

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
