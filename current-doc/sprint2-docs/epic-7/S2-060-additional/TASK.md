Task 1: Fix ingestion pipeline for MaterialEntry (BUG-002)

 Суть проблеми

 arq_ingest_material використовує SourceMaterialRepository (таблиця source_materials), але матеріали створені через
 /nodes/{node_id}/materials — це MaterialEntry (таблиця material_entries). Worker крашиться: SourceMaterial not found.

 Вторинний баг: після краша error handler намагається queued → failed — заборонений transition.

 Зміни

 1. src/course_supporter/storage/job_repository.py — дозволити queued → failed
 - Рядок ~15: додати "failed" до JOB_TRANSITIONS["queued"]

 2. src/course_supporter/api/tasks.py — dual-model detection в arq_ingest_material
 - Lazy import MaterialEntryRepository
 - Спочатку entry_repo.get_by_id(mid) — якщо знайдено → new model path
 - Якщо None → fallback на SourceMaterialRepository (backward compat)
 - New model: set_pending(mid, jid) замість update_status(mid, "processing")
 - Оновити type hints _resolve_s3_url: SourceMaterial → _HasSourceUrl protocol (MaterialEntry має source_url)
 - Передати is_new_model flag у callback

 3. src/course_supporter/ingestion_callback.py — branching по is_new_model
 - Додати is_new_model: bool = False до on_success() і on_failure()
 - on_success + new model: entry_repo.complete_processing(mid, processed_content=content_json,
 processed_hash=sha256(content_json))
 - on_success + old model: залишити mat_repo.update_status(mid, "done", content_snapshot=...)
 - on_failure + new model: entry_repo.fail_processing(mid, error_message=...)
 - on_failure + old model: залишити mat_repo.update_status(mid, "error", ...)

 4. Тести
 - tests/unit/test_api/test_ingestion_task.py — додати тести для MaterialEntry path + fallback
 - tests/unit/test_ingestion_callback.py — додати тести для is_new_model=True
 - Job repository test — queued → failed transition

 Verification

 uv run pytest tests/unit/test_api/test_ingestion_task.py tests/unit/test_ingestion_callback.py -v
 uv run mypy src/course_supporter/api/tasks.py src/course_supporter/ingestion_callback.py
 uv run pytest  # full suite
