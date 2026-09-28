# Зондування А3 — задача 13 «Знімок, різниця й дедуплікація для всіх типів; квитанція на ревізії»

Лише читання коду. База: `origin/main` @ `820c631`. Шляхи — від `src/course_supporter/`, якщо не сказано інше. Архітектури тут немає: тільки факти, варіанти, що прямо випливають із коду, і відкриті питання.

---

## 1. Нормалізатор і різниця

### 1.1 Де лежать і що публічне

Пакет `normalizer/`; експорт — `normalizer/__init__.py:46-72`.

| Символ | Файл:рядок | Вхід → вихід | Чиста? |
|---|---|---|---|
| `normalize_archive` | `normalizer/core.py:61` | `raw: bytes`, `archive_kind: "zip"\|"tar.gz"`, `limits`, `classifier` → `NormalizedSnapshot(manifest, canonical_zip, snapshot_hash)` | Без мережі, БД і S3. Один побічний вхід: `extract_archive_safely` читає `get_settings().safety_archive_max_files` (`security/archive.py:366`) |
| `compute_delta` | `normalizer/delta.py:15` | `(base: Manifest, sub: Manifest)` → `Delta(changed, new, deleted, hygiene_new_excluded)` | так, «Pure function, zero I/O» (:4) |
| `compute_aggregate_hash` | `normalizer/hashing.py:32` | `included` → SHA-256 від `sorted("path:hash")`, з'єднаних LF | так |
| `canonicalize_path` | `normalizer/hashing.py:19` | `str(PurePosixPath(arcname))` | так |
| `write_canonical_zip` | `normalizer/canonical_zip.py:29` | `(path, bytes)[]` → відтворюваний zip (`ZIP_STORED`, епоха 1980) | так |
| `manifest_to_jsonb` / `manifest_from_jsonb` | `normalizer/serde.py:35`, `:50` | `Manifest` ↔ JSONB (`schema: 1`) | так |
| `DefaultTextExtractor.extract` | `normalizer/extract.py:30` | `(EntryClass, raw)` → `str \| None` | так |
| `ExtensionClassifier`, `denylist_prefix`, `collapse_denylist`, `KNOWN_EXTENSIONS` | `normalizer/classify.py:101`, `:173`, `:196`, `:88` | — | так |
| `_PROJECT_NORMALIZE_LIMITS` | `normalizer/models.py:91` | 200 MB розпакування / глибина 1 / 100 MB очищеного | — |

Моделі даних (`normalizer/models.py`): `EntryClass` (:18; `text`/`document`/`binary`), `ExcludedReason` (:29), `NormalizerLimits` (:50), `ManifestEntry` (:99; `path`, `size`, `hash`, `cls`), `Manifest` (:143), `NormalizedSnapshot` (:188), `Delta` (:208).

### 1.2 Межі

- **Відбиток — від сирих байтів.** `ManifestEntry.hash` — «SHA-256 of the raw bytes» (`models.py:111`), обчислюється як `compute_raw_hash(entry.content)` (`core.py:156`). Сама «нормалізація» зачіпає лише **шляхи й склад архіву**: канонічний шлях, NFKC імені, відкидання denylist, вердикти. **Вміст файлів не нормалізується**: кодування, BOM, CRLF/LF, NFC не впливають на відбиток. Два файли, що відрізняються лише кінцями рядків, — це `changed`.
- **Двійкові файли.** Невідоме розширення (зокрема Makefile, Dockerfile, dotfiles) лишається в маніфесті як `BINARY` і відстежується за відбитком (`core.py:141-161`, `classify.py:104-110`). Текст із нього не видобувається (`extract.py:37`).
- **Кодування.** `DefaultTextExtractor`: TEXT → `decode("utf-8", errors="replace")` (`extract.py:31-32`). `recover_text` сюди не підключено.
- **Документи.** docx/pdf зберігаються як байти; текст видобувається «at diff-time» (`extract.py:3-8`).
- **Архіви.** Вхід — завжди архів: «the top-level input is always an archive (single-file submissions are rejected upstream), so no in-library docx/pdf guard exists here» (`core.py:10-14`). Вкладені архіви не відкриваються (`NESTED_ARCHIVE`, excluded).
- **Розмір.** Рівень 1 — `raw_max_unzipped_bytes`, рівень 2 — `kept_total_max_bytes`; перевищення рівня 2 відкидає весь архів (`core.py:196-201`). Ліміту на окремий файл немає (`models.py:55-60`).
- **Помилки.** `NormalizerError` на дубльований канонічний шлях (`core.py:144-148`), `NormalizerLimitError`, `SecurityRejectedError` зі структурних перевірок.

### 1.3 Чи працює різниця для будь-яких двох знімків

- `compute_delta` порівнює **два довільні `Manifest`** за `included`-шляхами й відбитками (`delta.py:37-54`). Вона нічого не знає про «базу» чи «проєкт»; назви `base`/`sub` — лише імена параметрів. Порожня база → усе `new` (`delta.py:33-35`). Тобто для двох ревізій (`sub_prev`, `sub_now`) функція придатна без змін.
- **Передумова:** обидва маніфести мають бути з `normalize_archive`, бо унікальність шляхів гарантує лише він (`delta.py:5-8`). Для однофайлової подачі маніфесту сьогодні не будується, і `normalize_archive` не приймає файл, що не є архівом (`core.py:10-14`).
- `Delta` — **лише шляхи**, без текстових різниць. Текстову різницю (unified diff) робить `homework/mentor_context.py::_unified_diff` (:385) при збиранні контексту, для змінених файлів понад `H_C_WHOLE_MAX_BYTES = 64 KiB` (:52). Менші файли показуються цілком.
- `build_mentor_context` (`homework/mentor_context.py:66`) — чиста функція над `(base_manifest, sub_manifest, delta, read_text, base_version, latest_version)`. У ній захардкожено семантику «база автора»: рядок «staleness» за версією бази (:362), «neighbour» — незмінені файли **бази**, на які посилаються змінені (:186).

---

## 2. Що зберігається сьогодні

### 2.1 Таблиця `homework_submissions` (`storage/orm.py:1452-1648`, `HomeworkSubmission`)

| Колонка | Рядок | Що |
|---|---|---|
| `file_url` | :1499 | S3/B2-шлях сирого файла |
| `file_type`, `original_filename` | :1502, :1507 | заявлений MIME, ім'я |
| `file_hash` | :1508 | SHA-256 **сирих байтів**, індекс, «for deduplication» |
| `safety_result` / `sanity_result` / `review_result` | :1528 / :1531 / :1539 | JSONB |
| `review_markdown`, `score` | :1547, :1550 | — |
| `student_note` | :1584 | — |
| `job_id` | :1592 | — |
| `base_id`, `snapshot_key`, `snapshot_hash`, `snapshot_manifest` | :1601-1626 | «NULL for non-project submissions. Populated by P3.» |
| `created_at` | :1629 | — |

**Номера спроби, посилання на попередню ревізію, колонки з прочитаним текстом немає.**

### 2.2 S3

- **Сирий файл:** `homework/{tenant_id}/{submission_id}/{filename}` (`homework/submission_core.py:335`), де `submission_id = uuid.uuid4()` (:333). Це **не** той `id`, який запис отримає в БД (там `_uuid7`, `orm.py:1471`).
- **Відповіді тесту:** `homework/{tenant_id}/{uuid4}/answers.json` (`submission_core.py:421`).
- **Знімок проєкту:** сусідній ключ `…/snapshot.zip` (`homework/project_submission.py:78-84`), запис — `:139-148` через `HomeworkRepository.store_snapshot` (`storage/homework_repository.py:480`).
- **Знімок бази проєкту:** `…/v{n}/snapshot.zip` (`workers/base_normalize.py:80-82`, `:177-178`), таблиця `project_bases` (`ProjectBase`, `orm.py:1651`: `version`, `archive_key`, `snapshot_key`, `snapshot_hash`, `manifest`, `state`).

### 2.3 Нормалізований / прочитаний текст

- **Ніде не зберігається.**
- Однофайлова подача: `Stage1Result.nfc_text` і `submission_text` живуть лише в пам'яті воркера. У `safety_result` сьогоднішній Ментор кладе тільки `not_opened` і `recovered_encoding` (`api/tasks.py:1111`, `:1115`).
- Проєкт: у `snapshot.zip` лежать **сирі байти** включених файлів, а не текст (`canonical_zip.py:1-8`); текст видобувається при збиранні контексту.
- `ExternalServiceCall.input_text` (`orm.py:1253`) пишеться, лише коли піднято налаштування повного входу, «which production refuses to boot with». Є `input_hash` (:1247) — відбиток відрендереного входу виклику моделі, не подачі.

### 2.4 За типами й шляхами

| Тип | Шлях сьогодні (`config/submission_paths.yaml`) | Сирий файл | `file_hash` | Знімок / маніфест |
|---|---|---|---|---|
| short_task | todays_mentor | так | сирих байтів | ні |
| task | todays_mentor | так | сирих байтів | ні |
| project | todays_mentor | так (архів) | сирих байтів **архіву** | `snapshot_key/hash/manifest` + `base_id` |
| test | new_path | `answers.json` (канонічний JSON) | від канонічного JSON (`submission_core.py:428`) | ні |

Для project обидва шляхи викликають ту саму `process_project_submission` (`api/tasks.py:1002-1018`, `homework/path_runner.py:445-456`), тож зберігання однакове.

---

## 3. Модель ревізій

- **Явного зв'язку між спробами немає.** Ні номера, ні `previous_submission_id`. Спроби пов'язує лише пара `(student_id, authored_document_id)` і порядок за `created_at`.
- Запити, що є:
  - `HomeworkRepository.list_for_student_and_task` (`storage/homework_repository.py:306`) — спроби на завдання, від найновішої, без soft-deleted; використовується на порталі;
  - `HomeworkRepository.has_reviewed_revision` (:184) — чи була вже рецензія (`completed`/`delivered`, без soft-deleted);
  - `HomeworkRepository.get_for_student` (:252) — у межах курсу.
- **Новий шлях.** `resolve_submission_state` (`homework/path_selection.py:66-90`) повертає `FIRST` або `REPEAT_WITHOUT_REPLIES`. `REPEAT_WITH_REPLIES` не повертається ніколи — до задачі 12 (:78-83, `path_config.py:92-102`). Попередню ревізію як об'єкт новий шлях **не** шукає: лише булеве «чи була рецензія».
- **Сьогоднішній Ментор.** `MentorReviewService._history` (`homework/review_graph.py:332-367`) бере `get_for_student` у межах курсу, обмежує `MENTOR_HISTORY_TASK_DEPTH = 3` завданнями (:96), лишає лише рецензовані й передає компактну проєкцію: `submission_id`, `score`, `correctness`, `same_task`, `weaknesses` з шарів. **Самі файли або текст попередньої спроби не читаються.**
- «Ревізія» в коді `path_continuation.py` / `path_checkpoint.py` — це **одна подача, що продовжується новим Job-ом**. Контрольна точка читається з останнього Job-а цієї подачі (`path_checkpoint.py:26-28`), а не з попередньої подачі.

---

## 4. Дедуплікація сьогодні

- **Єдина перевірка** — `HomeworkRepository.find_duplicate` (`storage/homework_repository.py:146-182`): той самий `student_id` + `authored_document_id` + `file_hash`, статус `completed`/`delivered`, найновіша. **Без** фільтра `deleted_at` — свідомо не змінено (:216-220).
- **Де:** `_record_and_dispatch` (`homework/submission_core.py:501-521`), до створення запису. Якщо знайдено — сирий об'єкт видаляється з S3, повертається `SubmissionDispatch(duplicate=True)` з наявною подачею і її `job_id`. Нова подача, Job і виклик моделі не створюються. Маршрути повертають `duplicate=True` (`api/routes/homework.py:274-280`, `api/routes/portal_submissions.py:273-277`).
- **Для тестів вимкнено:** `deduplicate=False` (`submission_core.py:450`, «every attempt at a test is scored anew»).
- **Проєкт:** дедуплікація теж за `file_hash` сирого **архіву**. Переархівований той самий проєкт (інші мітки часу в zip) дасть інший хеш. `snapshot_hash` (нечутливий до мітки часу, порядку й denylist-сміття — `hashing.py`, `canonical_zip.py`) для дедуплікації подач не використовується: лише для звірки з базою (`project_preflight`, `submission_core.py:194`, `:263-290`; `ProjectBaseRepository.find_by_snapshot_hash`).
- **Дедуплікації за відновленим / нормалізованим текстом немає.** Тотожна за змістом робота в іншому кодуванні, з BOM чи з CRLF — це нова спроба з повною ціною.
- Дедуплікація спрацьовує лише проти **рецензованої** спроби. Дві однакові подачі поспіль, поки перша ще в обробці, обидві підуть у роботу.

---

## 5. Знімок і різниця за типами

| Тип | Що є | Чого бракує |
|---|---|---|
| **project** | маніфест + `snapshot.zip` + `snapshot_hash` на кожну подачу; `compute_delta` проти бази (виводиться «on read», не зберігається — `project_submission.py:14-15`, :168) | різниця «проти попередньої ревізії» (попередній `snapshot_manifest` є в БД — можна взяти); збереженої квитанції немає; `build_mentor_context` знає лише «базу» |
| **task / short_task** (одиночний файл або архів) | сирий файл, `file_hash`, `Stage1Result` у пам'яті | маніфесту/знімка немає; `normalize_archive` не приймає одиночний файл (`core.py:10-14`); архів тут іде через Stage 1, а не через нормалізатор, тобто інший набір правил (див. звіт А2 §4) |
| **test** | канонічний JSON відповідей (`homework/test_doors.py:252-271`, `stored_answers`: відсортовані ключі, тотожні відповіді → тотожні байти); `file_hash` цих байтів | дедуплікацію вимкнено свідомо; «різниця» природно означала б порівняння `answers` за номерами питань — такої функції немає; `version_id` у JSON може відрізнятися між спробами |

Варіанти, що прямо випливають із коду (без рекомендації):
- (а) однофайлову подачу подати нормалізатору як маніфест з одного запису. Потрібен вхід, що не є архівом: `normalize_archive` сьогодні приймає лише архів.
- (б) для task/short_task відбиток брати від `Stage1Result.nfc_text` (відновлений NFC-текст), а не від сирих байтів. Колонки під нього немає.

---

## 6. Куди могла б лягти квитанція різниці — наявні місця

- **`ReviewStructureV1`** (`models/review_structure.py:316`) зберігається в `homework_submissions.review_result` (JSONB), розпізнається за версією (`api/routes/_portal_shared.py:40-67`, `curated_structure`). Секції:
  - `verdict` (:342), `test` (:343);
  - `fixed` / `new_remarks` / `open` / `broken` (:346-356) — вже семантика «проти попередньої ревізії», але на рівні зауважень, не файлів;
  - `mentor_voice`, `replies` (:362), `verification` (:365; `by_run`/`by_reading`), `progress`.

  Поля для квитанції / змінених файлів **немає**. `extra="forbid"`, тож нове поле — зміна схеми. Модуль зазначає: «every stored review is pre-rebuild, so the field is empty on every row in production» (:10-12).
- **Контрольна точка нового шляху** `PathCheckpoint` (`homework/path_checkpoint.py:140-175`) — JSON у `Job.stage_progress`, `extra="forbid"`. Поля: `task_type`, `submission_state`, `stages`, `frozen_*`, `retries`. Прив'язана до Job-а, а не до подачі.
- **Поля подачі:** `snapshot_manifest` (JSONB) + `snapshot_hash` + `snapshot_key` — наповнені лише для project. `safety_result` уже використовується як «носій» додаткових фактів Stage 1 (`not_opened`, `recovered_encoding`).
- **Сьогоднішній Ментор:** `review_result` = `ReviewResult` (`models/mentor_review.py`: `layers`, `aggregate_score`, `history_reconciliation`, `score_signals`, `verdict`) — інша схема, не V1.

**Для правила 2 даних немає.** Вердиктів по критеріях не зберігається. `TaskCriteria` (кеш декомпозиції на версію завдання, `homework/criteria_cache.py:1-30`) — це **перелік** критеріїв, без статусу «зелений/ні» на подачу. `Layer` має лише `strengths`/`weaknesses` рядками (`models/mentor_review.py:43-56`). Перевірки кодом (запуск тестів студента) не знайдено: grep `subprocess|docker|sandbox_run` у `homework/` та `agents/` порожній. `Verification.by_run` — лише поле схеми.

---

## 7. Тести

**Нормалізатор** (`tests/unit/test_normalizer/`):
- `test_delta.py`: `TestBasicSets` (`test_identical_is_empty_delta`, `test_changed_is_hash_inequality`, `test_same_hash_not_changed`, `test_new`, `test_deleted`, `test_mixed`), `TestHygiene`, `TestEdges` (`test_empty_base_all_new`, `test_path_in_both_deleted_and_hygiene`, `test_outputs_sorted`);
- `test_hashing.py`: `TestCanonicalizePath`, `TestAggregateHashDeterminism` (`test_order_invariant`, `test_leading_dot_slash_variant_same_hash`, `test_byte_exact_no_trailing_newline`, `test_cls_does_not_affect_hash`);
- `test_core.py`: `TestCleanProject`, `TestDenylist` (`test_denylist_junk_is_hash_neutral`), `TestVerdictMapping` (`test_non_whitelisted_ext_included_as_binary`), `TestDegenerateAndLimits` (`test_no_per_file_size_filter_exists`), `TestEdges` (`test_tar_gz_same_files_same_snapshot_hash`, `test_mislabeled_kind_raises_security_error`);
- `test_canonical_zip.py`: `TestByteReproducibility`, `TestPinnedZipInfo`, `TestRoundTrip`;
- `test_serde.py`, `test_extract.py` (`test_undecodable_bytes_replaced_not_raised`, `test_same_bytes_same_text_deterministic`), `test_classify.py`, `test_models.py`.

**Контекст і проєкт:**
- `tests/unit/test_mentor_context.py` (`test_changed_small_is_whole_large_is_diff`, `test_no_base_is_all_new`, `test_body_cannot_forge_structural_boundary`, `test_unreadable_body_becomes_marker`);
- `tests/unit/test_project_submission.py`;
- `tests/integration/test_project_submission_worker.py` (`test_ready_persists_snapshot_and_returns_rich_context`, `test_base_delta_changed_new_deleted`, `test_three_branch_delta_reaches_stages_unchanged`);
- `tests/storage/test_project_base_repository.py`.

**Дедуплікація й ревізії:**
- `tests/unit/test_api/test_homework_submit.py:804` `test_duplicate_file_returns_cached`;
- `tests/unit/test_api/test_portal_submit.py:212` `test_duplicate_returns_existing`;
- `tests/integration/test_test_doors_db.py:489-505` (тести не дедуплікуються);
- `tests/integration/test_path_selection_db.py`: `TestSubmissionState` (`test_a_reviewed_revision_makes_the_next_one_a_repeat`, `test_a_soft_deleted_review_does_not_count`, `test_a_review_on_another_task_does_not_count`) і `TestChoosePath` (`test_the_replies_state_is_never_chosen`).

**Схема рецензії:** `tests/unit/test_review_structure.py` (докстрінги `review_structure.py` запускаються там само).

**Прямого тесту `find_duplicate` на рівні репозиторію** (з БД) не знайдено — лише через маршрути з підробками.

---

## 8. Ризики, несподіванки, обсяг

**Несподіванки:**
1. **«Нормалізація» не нормалізує текст.** Відбиток — від сирих байтів; CRLF, BOM і кодування роблять ту саму роботу «іншою». Правило 1 («тотожна робота після нормалізації») на нинішньому відбитку спрацює лише на байт-у-байт копіях. Відкрите питання задуму «дедуплікація за відновленим текстом» — такої немає.
2. **`normalize_archive` не приймає одиночний файл** (`core.py:10-14`), а task/short_task-архіви йдуть через Stage 1, не через нормалізатор. Два різні контури читання архівів із різними білими списками (`HOMEWORK_POLICY` проти `KNOWN_EXTENSIONS`).
3. **Між ревізіями немає зв'язку, а прочитаний текст ніде не зберігається.** Щоб порівняти з попередньою ревізією однофайлової подачі, її треба знову завантажити з S3 і знову прочитати через Stage 1 (не з'ясовано, чи результат детермінований; `recover_text` залежить від `languages`, а вони — від мови рецензії).
4. **Дедуплікація проєкту за сирим архівом** майже не спрацьовує на повторному пакуванні; `snapshot_hash` для цього вже є, але не використовується.
5. **`find_duplicate` не фільтрує soft-deleted і шукає лише рецензовані.** Поведінку при паралельних однакових подачах не визначено.
6. **Для правила 2 немає вердиктів по критеріях** і перевірки кодом (§6). Правило 2 фактично залежить від інших задач (08+, стадії рецензії нового шляху); окремо в межах 13 його не реалізувати.
7. **`build_mentor_context` зав'язаний на «базу автора»** (staleness, neighbour). Для «проти попередньої ревізії» семантику доведеться розвести. Функція чиста, тож варіант із параметром випливає прямо.
8. **Сьогоднішній Ментор проти нового шляху.** Task/project ще на `todays_mentor`. Правило 3 (що бачить модель) вимагає змін у збиранні `submission_text` на обох дорогах або лише на новому (DD-SP-AM, див. А2).
9. `submission_id` у S3-ключі (uuid4) не збігається з `HomeworkSubmission.id` (uuid7) — `submission_core.py:333` проти `orm.py:1471`. Зв'язок лише через `file_url`.
10. **Схеми з `extra="forbid"`** (`ReviewStructureV1`, `PathCheckpoint`): квитанцію не докласти мовчки — потрібна зміна моделі, а для нової колонки ще й міграція (CLAUDE.md: «migration + tests + vision consistency check»).

**Оцінка обсягу (задача позначена M):**
- Знімок + квитанція «проти попередньої ревізії» лише для **project** (попередній `snapshot_manifest` у БД + `compute_delta` + місце для квитанції) — **S-M**.
- Поширення на task/short_task (маніфест одного файла, нормалізація вмісту для правила 1, збереження тексту/знімка, міграція) — ще **M**.
- test (різниця відповідей, політика дедуплікації) — **S**, але суперечить рішенню задачі 07 (decision 7, «every attempt is scored anew»).
- Правила 2 і 3 у повному обсязі — **L**, бо залежать від вердиктів по критеріях і стадій рецензії нового шляху, яких ще немає.

У сформульованому обсязі задача 13 **більша за M**. M реалістична, якщо звузити її до «знімок + відбиток + квитанція для всіх типів і правило 1», без правил 2-3.

**Відкриті питання для людини:**
1. Правило 1: тотожність за сирими байтами (є) чи за нормалізованим текстом (NFC, LF, без BOM, після `recover_text`) — і для яких типів?
2. Де зберігати знімок однофайлової подачі: реюз `snapshot_key/hash/manifest` (зараз «NULL for non-project») чи окрема колонка / таблиця ревізій?
3. «Попередня ревізія» — остання за `created_at`, остання рецензована чи остання не відхилена? Як бути з soft-deleted?
4. Квитанція — поле в `ReviewStructureV1` (для студента) чи службова колонка / JSON (для моделі й аудиту)?
5. Test: чи скасовується рішення 07 «кожна спроба оцінюється заново»?
6. Правило 1 на сьогоднішньому Менторі теж, чи лише на новому шляху?
7. Проєкт: різниця «проти попередньої ревізії» замість «проти бази» чи на додачу до неї (дві квитанції)?
8. Чи переводити дедуплікацію проєкту на `snapshot_hash`?
