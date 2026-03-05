Task 2: Add file upload to node materials endpoint (Q-002)

 Суть проблеми

 POST /courses/{id}/nodes/{nid}/materials приймає тільки JSON. Для локальних файлів (PDF, PPTX, MD) немає upload
 mechanism. Production використовує B2 — прямого доступу нема.

 Зміни

 1. Створити src/course_supporter/api/upload_validation.py — shared utilities
 - Перенести ALLOWED_EXTENSIONS dict з courses.py:70-76
 - Перенести _file_extension() з courses.py:58-62 → публічна file_extension()

 2. src/course_supporter/api/routes/courses.py — використати shared module
 - Замінити локальні ALLOWED_EXTENSIONS / _file_extension на import з upload_validation

 3. src/course_supporter/api/routes/materials.py — додати file upload
 - Змінити signature з JSON body (MaterialEntryCreateRequest) на Form fields + UploadFile:
   - source_type: SourceType (Form, required)
   - source_url: str | None (Form, optional)
   - file: UploadFile | None (Form, optional)
   - filename: str | None (Form, optional override)
 - Додати S3Dep injection
 - Валідація: source_url або file обов'язковий; web не приймає file; extension check
 - Upload: s3.upload_smart(upload_file_chunks(file), key, content_type, file_size)
 - S3 key: {course_id}/{node_id}/{uuid4()}/{filename}

 4. Тести
 - Оновити існуючі тести в tests/unit/test_api/test_materials.py — form-data замість JSON
 - Додати: file upload PDF, invalid extension 422, web + file 422, no url + no file 422
 - Додати mock_s3 fixture
 - tests/unit/test_api/test_upload_validation.py — тести для shared utils

 Verification

 uv run pytest tests/unit/test_api/test_materials.py tests/unit/test_api/test_upload_validation.py -v
 uv run pytest tests/unit/test_api/test_courses.py -v  # regression check
 uv run mypy src/course_supporter/api/
 uv run pytest  # full suite
