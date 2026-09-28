# Зондування А2 — задача 11 «Одні двері для обох доріг»

Лише читання коду. База: `origin/main` @ `820c631`. Шляхи — від `src/course_supporter/`, якщо не сказано інше. Архітектури тут немає: тільки факти, варіанти, що прямо випливають із коду, і відкриті питання.

---

## 1. Карта стадії безпеки

### 1.1 Файли й публічні функції (`security/`)

| Файл | Що в ньому | Публічне |
|---|---|---|
| `security/stage1.py` | оркестратор Stage 1 | `run_stage1` (:205), `screen_text` (:341), `Stage1Result` (:144), `archive_kind_for_filename` (:688), тип `DocumentTextExtractor` (:396) |
| `security/stage2.py` | модельна перевірка безпеки | `run_stage2_safety_check` (:77) |
| `security/policies.py` | політики контекстів | `AUTHORED_POLICY` (:217), `HOMEWORK_POLICY` (:326), `HOMEWORK_CONVEYORS` (:319), `CODE_EXTENSIONS` (:74), `policy_for` (:349), `get_max_size_for_extension` (:372) |
| `security/archive.py` | безпечне розпакування | `extract_archive_safely` (:280), `ExtractedFile` (:145), `ClassifiedEntry` (:197), `EntryVerdict` (:167) |
| `security/file_type.py` | тип файла | `extension_of` (:162), `verify_extension_matches_content` (:184), `detect_mime_type` (:119), `detect_charset` (:138) |
| `security/charset_recovery.py` | відновлення кодування | `recover_text` (:126) |
| `security/unicode_check.py` | жорстка відмова за Unicode | `check_text_unicode_safety` (:99) |
| `security/regex_patterns.py` | попередній фільтр ін'єкцій | `match_text` (:216), `PROMPT_INJECTION_PATTERNS` |
| `security/normalization.py` | NFC/NFKC | `nfc_for_storage` (:34), `nfkc_for_security` (:44), `normalize_filename` (:57) |

Текстові екрани (`check_text_unicode_safety`, `match_text`, `recover_text`) у всьому `src/` викликаються **лише** зі `stage1.py` (:776, :821, :823). Єдиний виняток — `ingestion/code.py:258`: тільки `recover_text`, без Unicode-перевірки й регулярних виразів.

### 1.2 Порядок кроків `run_stage1` (`stage1.py:254-338`)

1. `extension_of(filename)`: порожнє розширення → `FORBIDDEN_TYPE` (:257-262).
2. Білий список `policy.allowed_extensions` (:264-268).
3. Ліміт розміру `get_max_size_for_extension` (:270-279).
4. `verify_extension_matches_content`: magic; порожній вміст → `MAGIC_MISMATCH` (:281).
5. Розгалуження (:284-318):
   - **архів** → `_handle_archive_input` (:409);
   - **документ** (docx/pdf, лише для homework) → `_handle_document_input` (:578): структурна перевірка docx (:650), екстрактор, `_screen_text`;
   - **текст** (`_TEXT_EXTENSIONS = _PROSE | CODE_EXTENSIONS`, :137) → `_run_text_content_checks` (:724).
6. `_run_text_content_checks`: `recover_text` → `CHARSET_VIOLATION` (:776-788) → `_screen_text` (:802): NFKC → зняття одного BOM на початку → `check_text_unicode_safety` → `match_text` → `PROMPT_INJECTION` → NFC.
7. Кожна відмова пишеться в журнал як `stage1.rejected` (:330-338).

### 1.3 Режими, які вже є

| Режим | Вхід | Кроки |
|---|---|---|
| **файл** — `run_stage1(context=…)` | ім'я + байти | 1-7 |
| **текст** — `screen_text` (`stage1.py:341-385`) | `name` + байти | тільки п. 6: без розширення, розміру, magic і перевірки на порожнечу; `context` потрібен лише для журналу, політика не застосовується |
| **архів строгий** (authored) | — | перший непрочитаний член відкидає весь архів |
| **архів м'який** (`HOMEWORK_POLICY.archive_soft_exclude=True`, `policies.py:344`) | — | непрочитаний член іде в `not_opened`; `CHARSET_VIOLATION` члена → `not_opened`; `SUSPICIOUS_UNICODE`/`PROMPT_INJECTION` → відмова всій подачі (`stage1.py:535-551`) |

`screen_text` сьогодні кличуть лише для тестів 07c: `homework/test_yaml.py:313` (тіло без імені файла) і `:453` (поля чернетки), а також `api/routes/documents.py:626` через `screen_texts`. Для подач студента його **не** кличе ніхто.

### 1.4 Stage 2 (`stage2.py:77-210`)

Приймає готовий `submission_text` (NFC) і повертає `SafetyResult`. Нічого не нормалізує й не перевіряє регулярними виразами. Має дві підказки: `safety_check` / `safety_check_authored` (`content_kind`). Параметр `execution` додано для нового шляху (:170-175).

### 1.5 Таблиця «вхід → функція очищення → що перевіряється»

| Вхід | Функція | Перевіряється |
|---|---|---|
| одиночний текст/код подачі | `run_stage1(context="homework")` → `_run_text_content_checks` | розширення, розмір 1 MiB, magic/текстовість, кодування, Unicode, regex |
| docx/pdf подачі | `run_stage1` → `_handle_document_input` | розширення, розмір 10 MiB, magic, структура docx, екстракція, Unicode, regex (без кодування) |
| архів подачі (task/short_task) | `run_stage1` → `_handle_archive_input` (soft) | структура, бомба, глибина 3, білий список членів, magic членів, кодування/Unicode/regex текстових членів |
| архів проєкту | **не** Stage 1: `normalize_archive` (`normalizer/core.py:61`) | лише структура, бомба, `KNOWN_EXTENSIONS`, magic; текст **не** екранується (див. §4) |
| відповіді тесту | `check_test_answers` (`homework/test_doors.py:199`) | лише відповідність ключам питань/варіантів; вільного тексту немає |
| `student_note` | **немає** | — (див. §3.1) |
| матеріал автора (не code) | `run_stage1(context="authored")` (`api/routes/documents.py:807`, `:1059`) + Stage 2 authored (`api/tasks.py:416-422`) | файлові й текстові екрани + модель |
| матеріал автора `source_type=code` | Stage 1 свідомо пропущено (`documents.py:795-804`, `:1052-1056`) | лише Stage 2 authored + `recover_text` у `ingestion/code.py:258` |
| вебсторінка автора (`SourceType.WEB`) | лише Stage 2 authored (`api/tasks.py:416`); у `policies.py:9-13` прямо сказано «URL не моделюються» | тільки модель |
| тест автора (YAML / тіло / поля) | `run_stage1` / `screen_text` (`test_yaml.py:313-330`, `:453`) | текстові екрани |

---

## 2. Нинішні входи недовіреного тексту в рецензію

### 2.1 Двері подачі

- **Сторонній канал (API школи):** `POST /homework/submit` (`api/routes/homework.py`, `student_note` :125, передача :269) і `submit-test` (:368).
- **Портал:** `api/routes/portal_submissions.py` (`student_note` :142, :268, :352).
- Обидва ведуть у `homework/submission_core.py`: `validate_homework_file` (білий список дверей `ALLOWED_HOMEWORK_EXTENSIONS` = похідна від `HOMEWORK_POLICY`, :92-94), потім `create_and_dispatch_submission` і спільний хвіст (:470-560). У дверях екранування тексту **немає** — лише розширення й розмір. Уся безпека запускається у воркері.

### 2.2 Воркер: дві дороги

Точка розгалуження — `api/tasks.py:871-875`: `run_new_path_if_switched`. У `config/submission_paths.yaml` на новому шляху стоїть лише `test`; `task`, `project`, `short_task` — `todays_mentor`.

| Вхід | Сьогоднішній Ментор (`api/tasks.py`) | Новий шлях (`homework/path_runner.py::_open_the_doors`, :380) | Однаково? |
|---|---|---|---|
| одиночний файл / архів (task, short_task) | `run_stage1` (:1028-1035) + `assemble_submission_text` (:1041) | `run_stage1` (:459-466) + `assemble_submission_text` (:467) | Stage 1 однаковий. **Відмінності після нього:** новий шлях відкидає `not_opened` (`text, _not_opened`, :467) і не переносить `recovered_encoding`; сьогоднішній кладе обидва в `safety_result` (:1111, :1115) |
| проєкт | `process_project_submission` (:1002-1018), без Stage 1 | те саме (:445-456), без Stage 1 | однаково, і однаково без текстових екранів |
| тест | не доходить (тести на новому шляху) | `file_bytes.decode("utf-8")` без Stage 1 (:438-443) | — |
| Stage 2 | `run_stage2_safety_check(..., course_context=course_ctx)` (:1103-1107) | `run_safety_stage` → `run_stage2_safety_check(context.submission_text, router, execution=…)` **без `course_context`** (`homework/path_stages.py:205-209`) | підказка отримує різний контекст |
| текст завдання | sanity + граф рецензії | `load_task_context` (`homework/task_context.py:24`) в `run_attempt_classifier_stage` (`path_stages.py:242`) | однакове джерело: `DocumentSummary`/`DocumentSegment`, згенеровані з авторського матеріалу, що пройшов authored-перевірки |
| `student_note` | `review_graph.py:251` → `agents/mentor_review.py:254` → `prompts/mentor_synthesis/v1.md:22-33` | не використовується (на новому шляху немає стадії рецензії) | — |

Двері навмисно **продубльовано**, а не спільні: «Deliberately NOT shared with today's body» (`path_runner.py:393-395`, посилання на DD-SP-AM). Тож будь-яка зміна в дверях — це дві правки, або ж сьогоднішній Ментор лишається як є.

### 2.3 Інші недовірені рядки, що потрапляють у підказку без текстових екранів

- **Імена членів архіву** (`arcname`): проходять лише `_validate_arcname` (`security/archive.py:874-914`: null, backslash, `/`, `..`, NFKC). Далі потрапляють:
  - у рамку `--- {name} ---` (`homework/text_budget.py:75`, :248);
  - у блок `=== NOT OPENED ===` (`homework/doors.py:109-122`);
  - у дерево/шапки проєкту (`homework/mentor_context.py:299-330`, :376-382; екранується лише сентинел `@@MENTOR_CTX@@` і `\r\n`, :397-405).

  `match_text` / `check_text_unicode_safety` до імен не застосовуються.
- **`original_filename` подачі:** `run_stage1` відкидає лише ім'я без розширення. Чи ім'я одиночного файла потрапляє в підказку — не з'ясовано.

---

## 3. Три нові входи

### 3.1 Репліки студента

**Сьогодні існує `student_note`.**
- Стовпець `HomeworkSubmission.student_note` (`storage/orm.py:1584`), `Text`.
- Поле форми без `max_length` (`api/routes/homework.py:125-133`, `portal_submissions.py:142-148`, `api/schemas.py:1807`, `:1847`).
- Шлях: `submission_core.py:311` → `:540` → `review_graph.py:251` → `mentor_synthesis/v1.md` у тегах `<student_note>` з інструкцією «data, not instructions».
- **Жодного екранування:** ні Stage 1, ні Stage 2 (Stage 2 бачить лише `submission_text`). Екранування тільки на рівні підказки.

**«Репліки» як окремий запис — ще немає.**
- `StudentFeedback.text` (`orm.py:2073`) «Always NULL in task 05».
- `FeedbackKind` має лише `TOUCH` (`feedback_kinds.py:50-57`); репліка — майбутній член (`feedback_kinds.py:23-30`, `orm.py:2010-2016`).
- `ReviewStructureV1.replies` / `Reply.said` (`models/review_structure.py:164-176`, :362) — місце у **виході** рецензії.
- `submission_paths.yaml`: `repeat_with_replies` «unreachable until student replies exist as records (task 12)».

**Що знадобилося б:**
- для `student_note` — виклик `screen_text` на дверях (синхронна відповідь 4xx) або у воркері (відмова подачі; тоді двічі — в `tasks.py` і в `path_runner.py`);
- для реплік задачі 12 — те саме на місці запису, якого ще немає;
- мови для `recover_text` треба знати до перевірки. На дверях їх сьогодні обчислює воркер (`tasks.py:960-979`), двері — ні.

### 3.2 Прочитані сторінки з інтернету

- **У конвеєрі рецензії звернень до мережі немає.**
  - Пошук `httpx|aiohttp|requests.|urlopen|trafilatura|fetch_url` у `homework/` і `agents/mentor*`, `agents/sanity*` нічого не дав.
  - `tool_steps: 0` на обох стадіях (`config/submission_paths.yaml`; поле — `homework/path_config.py:143`).
  - Виконавця інструментів / циклу `tool_steps` у `llm/` не знайдено (grep `tool_steps` у `llm/` порожній).
- **Єдине читання вебу — авторське:** `ingestion/scrape_web.py::local_scrape_web` (:25, trafilatura), підключене через `ingestion/factory.py:47`, `:91` (`WebProcessor`). Воно проходить лише Stage 2 authored (`api/tasks.py:416-422`), без текстових екранів Stage 1.

**Що знадобилося б:** точки входу, куди підключати очищення, ще немає — вона з'явиться разом зі стадією, яка читає вебсторінки. Технічно готовий кандидат — `screen_text(name=url, content=…)`. Відкрите питання — режим (див. §6).

### 3.3 Файли проєкту без розширення (Makefile, Dockerfile)

Що з ними стається зараз:

| Де | Фільтр | Результат |
|---|---|---|
| одиночний файл «Makefile» | `validate_homework_file` + `run_stage1:257-262` (`extension_of` → `""`, `file_type.py:175-180`) | відмова `FORBIDDEN_TYPE` |
| член архіву task/short_task | `archive._yield_or_recurse:789-799` (`"" ∉ allowed_extensions`) → `EntryVerdict.FORBIDDEN_TYPE` → `stage1._handle_archive_input:479-495` | `not_opened` з причиною `forbidden_type`, ім'я названо в блоці NOT OPENED, тіло не читається |
| член архіву проєкту | `normalizer/classify.py::_extension_of` (:91-98, dotfile → `""`), `ExtensionClassifier` (:104-110) → `BINARY`; `core.py:141-161` лишає в маніфесті | у дереві `[binary, …]`; тіло `(body unavailable -- binary or non-extractable)` (`mentor_context.py:63`, :381); `DefaultTextExtractor` повертає `None` для BINARY (`normalizer/extract.py:30-37`) |

Так само «невидимі» `Dockerfile.worker` (розширення `worker`), `.gitignore`, `.env`, `*.lock`, `requirements.in` тощо — усе, чого немає в `KNOWN_EXTENSIONS` (`classify.py:88`).

Тести це фіксують:
- `tests/unit/security/test_archive_classify.py:169` `test_no_extension_becomes_forbidden_type`;
- `tests/unit/test_normalizer/test_classify.py:35`, `:41` (`Makefile`, `my.pkg/Makefile` → `BINARY`).

**Що знадобилося б, щоб такий файл читали й очищали.** Рішення «чи читати» сьогодні скрізь приймається лише за розширенням, у кількох місцях:
- `HOMEWORK_POLICY.allowed_extensions` / `HOMEWORK_CONVEYORS` (`policies.py:319-345`);
- `stage1._TEXT_EXTENSIONS` / `_is_text_extension` (`stage1.py:137`, :719-721);
- `archive._yield_or_recurse` (білий список, :789);
- `file_type.verify_extension_matches_content` (порожнє розширення → `FORBIDDEN_TYPE`, :232-237);
- `normalizer/classify.py` (`_TEXT_EXTS`, `KNOWN_EXTENSIONS`, `ExtensionClassifier`);
- `DefaultTextExtractor`.

Варіант, що прямо випливає з коду: список дозволених **імен** поруч зі списком розширень. Але «замки» побудовані саме на множинах розширень (§5), тож їх треба розширювати разом.

---

## 4. «Проєкт через двері»

**Потік** (однаковий на обох дорогах): `homework/project_submission.py::process_project_submission` (:98).

1. `archive_kind_for_filename` (:120).
2. `normalize_archive(..., limits=_PROJECT_NORMALIZE_LIMITS)` (:130-132) → `extract_archive_safely(classify=True, allowed_extensions=KNOWN_EXTENSIONS, skip_matcher=denylist_prefix)` (`normalizer/core.py:104-120`).
3. Denylist-згортання (`classify.py:115-193`).
4. Вердикти: `INCLUDED` і `FORBIDDEN_TYPE` лишаються (як text/document/binary), `MAGIC_MISMATCH` / `NESTED_ARCHIVE` виключаються (`core.py:139-194`). Далі ліміт рівня 2, канонічний zip.
5. Знімок у S3 + дельта з базою (:139-177).
6. `build_mentor_context`, де `read_text` = `DefaultTextExtractor.extract`, тобто `raw.decode("utf-8", errors="replace")` для TEXT (:184-207).
7. Ліміт розміру `project_context_budget_chars` (:218-234).
8. Результат іде прямо в Stage 2 → sanity → рецензію.

**Розбіжності з іншими подачами:**

| | task/short_task (Stage 1) | project (normalizer) |
|---|---|---|
| відновлення кодування | `recover_text` з мовами | немає: `decode(errors="replace")` |
| Unicode hard-reject | так | **ні** |
| regex-фільтр ін'єкцій | так | **ні** |
| docx/pdf усередині | `not_opened` (`stage1.py:499-515`) | читаються екстрактором (`extract.py:33-36`) без екранування |
| невідоме розширення | `not_opened` | лишається як `binary` без тіла |
| білий список | `HOMEWORK_POLICY` | `KNOWN_EXTENSIONS` (інший склад: `rst`, `ini`, `cfg`, `csv` є; `gz`/`tgz` немає) |
| межа | `submission_text_budget_chars` | `project_context_budget_chars` |
| формат відмови | `Stage1RejectionResult` (`source='stage1'`) | `{"source": "normalizer", "reason", …}` (`project_submission.py:248-276`) |
| тіло базового проєкту (авторське) | — | теж без текстових екранів (той самий `read_text`) |

Stage 2 на проєкті працює (модельна перевірка всього контексту). Жорстких детермінованих екранів немає. Обґрунтування обходу Stage 1 — `project_submission.py:1-7` і `tasks.py:981-986` («fail-closed on a real project's non-allowlisted files»). Тобто його обходять заради м'якості до типу файлів, а текстові екрани «випали» разом із ним.

---

## 5. Покриття тестами й замки

**Stage 1** — `tests/unit/security/test_stage1.py`:
- `TestTextChecks` (:352): ін'єкція, BOM, повноширинні символи;
- `TestCharsetEnforcement` (:437);
- `TestHomeworkSoftArchive` (:764), зокрема:
  - `test_injection_inside_archive_still_fails_the_submission` (:820);
  - `test_suspicious_unicode_inside_archive_still_fails_the_submission` (:830);
  - `test_structural_guards_are_not_softened` (:846);
- `TestDocumentConveyor` (:857), зокрема:
  - `test_injection_inside_a_docx_is_screened` (:887);
  - `test_documents_inside_an_archive_are_named_never_opened` (:983);
- `TestScreenText` (:1038), зокрема:
  - `test_an_empty_or_one_letter_text_is_not_refused`;
  - `test_as_a_file_the_same_text_is_refused_by_the_file_checks`;
  - `test_the_text_screens_still_refuse`;
  - `test_a_refusal_is_logged_as_stage1_logs_one`;
- `TestStructuredLogging` (:533), `TestIsTextExtension` (:719).

**Замки на побудову множин** — `tests/unit/security/test_policies.py`:
- `TestConveyorTable` (:335);
- `TestDoorMatchesPolicy` (:371);
- `TestTextExtensionsDerived` (:396, `test_text_conveyor_and_screened_set_agree`);
- `TestPolicyConsistency` (:271).

Нормалізатор: `tests/unit/test_normalizer/test_classify.py` (`TestCodeExtensionsAreText` згадано в `classify.py:80`).

Інше:
- формати дверей: `tests/unit/security/test_homework_formats.py`;
- модули безпеки: `test_archive_classify.py`, `test_unicode_check.py`, `test_regex_patterns.py`, `test_charset_recovery.py`;
- Stage 2: `tests/integration/test_stage2_safety_check.py`, `tests/unit/test_homework/test_path_stages.py` (`TestWhatIsHandedToTodaysFunction`, `TestTodaysCallersAreUnchanged`);
- доріжки: `tests/integration/test_homework_pipeline.py`, `tests/integration/test_new_path_pipeline.py` (`TestTodaysMentorIsNotDisturbed`), `tests/unit/test_homework/test_path_runner_reactions.py`, `tests/integration/test_path_e2e_stages_db.py`;
- проєкт: `tests/unit/test_project_submission.py`, `tests/integration/test_project_submission_worker.py`, `tests/unit/test_mentor_context.py`;
- `student_note` — лише рендер підказки / агент: `tests/unit/test_prompts/test_mentor_review_prompts.py:139`, `tests/unit/test_agents/test_mentor_review_agent.py:256`.

**Замок порядку кроків.** Окремого тесту, що ламається, коли переставити `recover_text → unicode → regex`, не знайдено. Порядок тримається кодом `_run_text_content_checks` / `_screen_text` і поведінковими тестами на кожну категорію; «в тому самому порядку» для `screen_text` сказано лише в докстрінгу (`stage1.py:354-355`). Спостереження: `test_injection_inside_archive_still_fails_the_submission` непрямо стереже, що регулярний вираз іде **після** м'якого відсіву кодування.

**Не покрито** (тестів не знайдено):
- ін'єкція / прихований Unicode у файлі **проєкту**;
- `student_note` з ін'єкцією;
- екранування імен членів архіву;
- розбіжність `course_context` / `not_opened` між двома дорогами.

---

## 6. Ризики, несподіванки, обсяг

**Несподіванки:**
1. **Проєкт іде в модель без жодного детермінованого текстового екрана** (§4), і на обох дорогах. «Проєкт через двері» — фактично найбільша частина задачі.
2. **`student_note` уже існує і вже йде в підказку рецензії** без екранування й без обмеження довжини (§3.1). «Новий вхід» (1) частково вже є.
3. **Новий шлях губить `not_opened` і `recovered_encoding`** (`path_runner.py:467`) і не передає `course_context` у Stage 2 (`path_stages.py:205`). Це вже розбіжність доріг; на `task` не видно лише тому, що перемикач вимкнено.
4. **Двері продубльовано навмисно** (DD-SP-AM, `path_runner.py:393-395`): зміна «одних дверей» торкає або сьогоднішнього Ментора, або тільки новий шлях.
5. `screen_text` відкидає за регулярним виразом **усе** — ні м'якого режиму, ні режиму «попередити». Для вебсторінок (стаття про prompt injection) і для реплік студента («ігноруй попереднє…» як жарт) можливі хибні відмови. Склад патернів — `security/regex_patterns.py`; хибні спрацьовування на реальних даних не вимірювано.
6. Вмикання екранів для проєкту змінює поведінку: сьогодні одна ін'єкція в будь-якому `.py` проєкту **не** відкидає подачу, а після зміни відкидатиме — за правилом м'якого архіву (`stage1.py:535-543`). Це продуктове рішення, не технічне.
7. Авторська сторона (code-матеріали, веб) теж без текстових екранів Stage 1, тож база проєкту, яку читає рецензія, неекранована. Чи «ті самі двері» охоплюють авторське — не з'ясовано.
8. `KNOWN_EXTENSIONS` нормалізатора і `HOMEWORK_POLICY.allowed_extensions` — дві різні множини; Makefile-правило доведеться класти в обидві.

**Що виходить за межі S:**
- проєкт через текстові екрани (нормалізатор + `mentor_context` + обидві дороги + відповідь з `source='normalizer'` + портальні формулювання) — **M**;
- файли без розширення в обох контурах із розширенням замків (§3.3) — **S-M**;
- вебсторінки — **поза задачею**, доки немає стадії, яка їх читає (підключати нікуди);
- репліки як записи — залежить від задачі 12 (міграція `student_feedback`).

**В межах S:** `screen_text` для `student_note` в одному місці + обмеження довжини + тест.

**Оцінка:** уся задача 11 у сформульованому обсязі — ближче до **M**, ніж до S. S досяжна, якщо звузити її до «публічна функція + `student_note` + файли без розширення лише в одному контурі».

**Відкриті питання для людини:**
1. Чи дозволено торкатися дверей сьогоднішнього Ментора (`tasks.py`), чи зміни лише в `path_runner.py` (DD-SP-AM)?
2. Проєкт: ін'єкція в одному файлі → відмова всій подачі (як м'який архів) чи «не відкрито» для цього файла?
3. «Два режими» очищення — це `run_stage1` (файл) / `screen_text` (текст), що вже є, чи щось інше (строгий/м'який; відмова/попередження)?
4. `student_note`: відмова на дверях (синхронна, 4xx) чи у воркері (статус `rejected`)? Звідки двері знатимуть мови для `recover_text`?
5. Файли без розширення: закритий перелік імен (Makefile, Dockerfile, …) чи «будь-який текстовий файл без розширення» за magic/charset?
6. Чи входить у задачу розбіжність доріг (`not_opened`, `course_context`) — чи це окремий борг?
7. Імена членів архіву в підказці — екранувати чи ні?
