Доручення: Ментор / зондування шляху проєкту · Для: сесія «Ментор»

# Зондування: шлях проєкту на новому Менторі

База — `main` на `80ab531` (PR #615). Лише читання коду; тести не запускались (залежності в
контейнері не встановлені, мережі до PyPI немає). Посилання — `файл::символ`, рядок лише орієнтир.

## Суть (10 рядків)

1. Сьогодні проєкт іде так: `api/tasks.py::arq_process_homework` → `process_project_submission` (нормалізатор, екран, знімок у S3, різниця з базою) → 6 платних драбин: safety, sanity, дві оцінки шарів, denoise, synthesis. Список критеріїв складається раз на версію задачі.
2. Двері нового шляху для project уже є і перевірені (`test_one_door_db`): `path_runner._open_the_doors` викликає ту саму `process_project_submission`. Safety й attempt_classifier на них працюють.
3. Блокер №1: для project `DoorReading.files` порожній, а `criteria_evaluation.evaluate_criteria` на порожньому `work` кидає `ValueError`.
4. Блокер №2: промпти `criteria_evaluation/v1` і `review_explanation/v2` описують рамки `--- path ---` і блок `NOT OPENED`. Проєкт має інші рамки: `@@MENTOR_CTX@@ FILE type=… path=…`, довірений блок і diff.
5. Блокер №3: у `result_builders.get_result_builder` немає project. Тип, перемкнутий без збирача, закінчується `completed` **без рецензії**, і перевірка конфігурації цього не ловить.
6. Ризик якості: якщо цитати шукати в усьому знімку, «met» може стояти на коді автора бази. Цитата мусить стояти в рядках студента.
7. `course_link_lookup` для проєкту готовий як є: він не залежить від форми роботи.
8. Бюджет входу проєкту (`text_budget.project_context_budget_chars`) рахується від драбин **сьогоднішнього** Ментора (`ladders_mentor.yaml`). Для нового шляху його треба рахувати від стадій `submission_paths.yaml`.
9. Для служби запуску в коді немає нічого: ні профілю запуску, ні команди тестів, ні розпізнавання тестів. Є знімок (повний проєкт студента), база з версіями і `Verification.by_run` у структурі рецензії.
10. «Спроба»: `snapshot_hash`, маніфест, `compute_delta` і `previous_verdicts` є. Немає номера спроби, пошуку «тієї самої роботи» за `snapshot_hash` (і індексу на ньому), посилання на попередню спробу, заповнення `fixed/open/broken`.

---

## а. Як сьогоднішній Ментор рецензує проєкт

**Подача (двері API).** `api/routes/homework.py` і `portal_submissions.py` → `homework/submission_core.py`.
- Межа завантаження проєкту — `PROJECT_SUBMISSION_MAX_UPLOAD_BYTES` = `api/upload_validation.py::PROJECT_ARCHIVE_MAX_UPLOAD_BYTES` = 100 MiB. Для одного файла — `MAX_HOMEWORK_SIZE`.
- Ехо бази: `base_snapshot_hash` звіряється з `ProjectBaseRepository.find_by_snapshot_hash`. Якщо база є, але не готова, — 409 `BASE_NOT_READY`. Якщо бази немає взагалі, різниця вважається «усе нове».
- Дедуплікація: `HomeworkRepository.find_duplicate` шукає за `file_hash` (SHA-256 **сирих байтів**) серед `completed/delivered`.
- Запис ревізії та постановка `arq_process_homework(job_id, submission_id)` у чергу.

**Робота воркера.** `api/tasks.py::arq_process_homework`.
- Першим кроком — `run_new_path_if_switched`. Для project він повертає `False`, нічого не читаючи.
- Далі файл завантажується з S3 (`s3.download_file`). Мова рецензії: `resolve_review_language`. Мови перевірки — курс плюс читач.
- Екран коментаря: `doors.screen_student_note`.
- Гілка `is_project` → `homework/project_submission.py::process_project_submission`.

**`process_project_submission`, крок за кроком:**
1. `normalize_archive(..., limits=_PROJECT_NORMALIZE_LIMITS)` (`normalizer/models.py`): розпаковано ≤200 MB, вкладеність 1, очищений знімок ≤100 MB. Перевищення відмовляє в усьому архіві (`NormalizerLimitError`), окремі файли не відкидаються. Відсів:
   - `normalizer/classify.py::_DENYLIST_EXACT/_DENYLIST_GLOBS` — `.git`, `.venv`, `node_modules`, `__pycache__`, `dist`, `build`, `__MACOSX` …;
   - файли з можливими секретами (`.env`, `*.pem`) → `ExcludedReason.MAY_CONTAIN_SECRETS`;
   - вкладені архіви → `NESTED_ARCHIVE`;
   - невідоме розширення → `EntryClass.BINARY`: лишається в маніфесті з хешем, але не читається.
2. `_screen_snapshot`: кожен TEXT/DOCUMENT-файл і кожне ім'я проходять крізь `security/text_screen.screen_text`.
   - Підміна напрямку тексту відмовляє в усій подачі.
   - Файл у чужому кодуванні потрапляє в `not_opened`.
   - Зламаний docx/pdf → `MAGIC_MISMATCH`.
   - Секрети названо в `not_opened`, але не прочитано.
3. Знімок: `snapshot.zip` поруч із сирим ключем у S3 (`_submission_snapshot_key`). Колонки `HomeworkSubmission.snapshot_key/snapshot_hash/snapshot_manifest` пише `store_snapshot`.
4. Різниця з базою автора: `compute_delta(base_manifest, sub_manifest)`. Буває `changed/new/deleted/hygiene_new_excluded`. Різниця **не зберігається**, лише логується (`project_submission.delta`).
5. Контекст: `homework/mentor_context.py::build_mentor_context`. Це чиста функція без I/O.
   - Довірений блок: дерево бази, дволівнева різниця, метрики F2, рядок застарілості версії бази.
   - Недовірені блоки `@@MENTOR_CTX@@ FILE type=…`: CHANGED-FULL (файл ≤64 KiB, `H_C_WHOLE_MAX_BYTES`), CHANGED-DIFF (unified diff), NEW (цілий), NEIGHBOR (незмінний файл бази, чиє ім'я згадує змінений файл).
   - **Незмінні файли бази без згадки в контекст не потрапляють.**
6. До кінця дописується `not_opened_block` (`=== NOT OPENED (N) ===`).
7. **Єдина межа розміру:** `len(context) > project_context_budget_chars()` → `rejected` з `category=over_budget`, ще до першого виклику моделі. Обрізання немає.
   - Бюджет = найменше вікно **перших** щаблів `STUDENT_LADDER_STAGES` × `input_budget_ratio` (0.5) × 2 символи на токен.
   - За нинішніх драбин це mistral-small-latest із вікном 131 072 токени, тобто ≈131 072 символи (числом не перевіряв, бо не запускав).
   - Окремої межі на кількість файлів чи токенів немає.

**Стадії та промпти (платні).** Драбини в `config/ladders_mentor.yaml`, грошових стель на них немає.

| стадія (ім'я в реєстрі) | промпт | щаблі | бачить роботу |
|---|---|---|---|
| `safety_check` (`security/stage2.run_stage2_safety_check`) | `prompts/safety_check/v1.md` | mistral-small → deepseek-flash → gemini-3.5-flash-lite | так, увесь контекст |
| `sanity_check` (`homework/sanity_gate`) | `prompts/sanity_check/v1.md` | ті самі | так |
| `criteria_decomposition` (`criteria_list_service.get_or_compose`) | `prompts/criteria_decomposition/v2.md` | deepseek-v4-pro (thinking) → qwen3.7-max → deepseek-flash | ні (лише задача); **раз на версію задачі** |
| `mentor_layered_evaluation_node_course` | `…_node_course/v1.md` | deepseek-v4-pro (thinking, 32 768 вих.) → qwen3.7-max → deepseek-flash | так |
| `mentor_layered_evaluation_industry` | `…_industry/v1.md` | те саме | так |
| `mentor_denoising` | `mentor_denoising/v1.md` | qwen3.7-max → gemini-3.8-flash → deepseek-flash | ні: шари й історія (≤3 задачі, `MENTOR_HISTORY_TASK_DEPTH`) |
| `mentor_synthesis` | `mentor_synthesis/v1.md` | те саме | ні: шари, бали, коментар студента |

Що коштує гроші. Шість драбин на кожну подачу плюс складання списку раз на версію задачі. Найдорожчі — дві оцінки шарів на deepseek-v4-pro з міркуванням: кожна читає до ~65 тис. токенів контексту (граф `homework/review_graph.py::MentorReviewService.review`, Phase 1 через `asyncio.gather`).

**Вихід.**
- `models/mentor_review.ReviewResult`: `layers` (node/course/industry — бал, слабкі місця, обґрунтування), `aggregate_score`, `history_reconciliation`, `score_signals`, `verdict` (за порогами, рахує код), `criteria_unavailable`.
- Плюс `review_markdown`, вільний текст синтезу від моделі, і `score` після `denoise`.
- Зберігає `store_review_result` у колонки `review_result` (JSONB), `review_markdown`, `score`.
- Статуси: `completed`, потім `webhook.build_reviewed_payload` / `deliver_webhook`, потім `delivered`.
- **Формат не той, що на новому шляху** (`ReviewStructureV1` + `assemble_review`).

## б. Стадії нового шляху для коду з кількох файлів

**Одні двері (`path_runner._open_the_doors`).** Для project викликається та сама `process_project_submission` (`test_one_door_db.py::TestAnAgentsProjectGoesThrough` на обох дорогах).
- Рецензент бачить `path=Makefile` / `path=Dockerfile`. `.env` потрапляє в `not_opened`. Прапорці екрана в текст моделі не потрапляють.
- Рамки `--- шлях ---` і відсів «від найменшого» (`text_budget.fit_archive_entries`) — лише для **архіву типу task** через Stage 1 (`TestAnArchiveGoesThroughTheSameDoor`).
- Проєкт відмовляється цілим за бюджетом. Окремих файлів у `NOT OPENED` він не скидає: туди потрапляють лише нечитані й секретні.
- Готове для project: двері, `safety`, `attempt_classifier`. Їх описано в `submission_paths.yaml::task_types.project`, на дверях вони перевірені.

| стадія | придатна для проєкту? | що змінити |
|---|---|---|
| складання списку (задача 08, `criteria_list_service`, `prompts/criteria_decomposition/v2.md`) | так: уже працює для project сьогодні (`review_graph` бере той самий список), є гілка `task_type == "project"` з `check_method: code_test` (`criteria_form.CheckMethod.CODE_TEST`, `soft_descent`) | Список **не бачить бази** (дерева файлів, тестів автора), лише текст задачі. Варіант: показати дерево бази й імена тестів. Але кеш списку ключований на `content_hash` документа, тож нова версія бази список не оновить. Рекомендація: у кроці 2 не чіпати, записати борг. |
| `criteria_evaluation` (`criteria_evaluation.py`, `criteria_verdicts.py`) | ні | (1) Двері мусять дати `files`: `WorkFile` за кожним файлом студента з `screen.texts`. (2) Промпт для рамок MENTOR_CTX: що таке довірений блок, які типи блоків, цитувати лише з NEW / CHANGED-FULL / рядків CHANGED-DIFF без маркера `+`/` `, ніколи з NEIGHBOR і довіреного блоку. (3) Правило «цитата студента» (нижче). `SubmittedWork.find` уже повертає `QuotePlace(file, lines)`, і колонки `quote_file/quote_line_start/quote_line_end` є. Багатофайловість готова. |
| `course_link_lookup` (`course_links.py`) | так, як є | Нічого. Шукає за концептами невиконаних критеріїв, роботи не читає. |
| `review_explanation` (`review_explanation.py`, `prompts/review_explanation/v2.md`) | майже | Промпт описує `--- path ---` (рядки ~65–72). Потрібен варіант під MENTOR_CTX. Факти вже несуть `place` (файл і рядки) виконаних цитат. Для невиконаного критерію місця в коді немає (див. в). |

**Цитата студента, а не автора.** Варіанти:
- (A) `WorkFile` з усього знімка. Ризик: «met» на коді бази.
- (B) Лише `changed + new`. Чесно, але критерій «не зламав наявне» не процитуєш.
- (C) Як B, плюс цитата, що є і в базовій версії того самого файла, не стоїть: нова `RepeatReason.QUOTE_FROM_BASE`, з одним повтором, як інші причини.

**Рекомендація: C.** Варіант A відкидаю: він дає хибні «met». Між B і C обираю C, бо правило дешеве (текст бази вже читається в `read_text`) і вкладається в наявний механізм повтору.

**Окрема назва стадій.** Рекомендую `project_criteria_evaluation` і `project_review_explanation` у `submission_paths.yaml`, з власними `prompt_ref` і стелями, і реєстрацію тих самих виконавців під новими іменами. Такий підхід передбачає сам файл: інша стеля — інше ім'я. Промпти текстових типів лишаються байт у байт (їх калібрування стоїть), проєкт калібрується окремо.

**Бюджет.** `project_context_budget_chars` читає `ladders_mentor.yaml::STUDENT_LADDER_STAGES`. Для нового шляху бюджет треба виводити з перших щаблів стадій типу `project` у `submission_paths.yaml`; сьогодні це дасть те саме число. Інакше правка драбини шляху бюджет не зсуне. Стеля `criteria_evaluation` (0.55 $) рахувалась для роботи в 131 072 символи. Проєкт має той самий бюджет, але довірений блок і diff-рамки — частина того самого бюджету, тож стелі не ростуть.

## в. Збирач рецензії проєкту

Переюзати з `text_result.py`:
- `build_text_review`: бал і `passed` рахує код (`score_percent`, `is_passed`), зауваги впорядковані за вагою;
- `REVIEW_PARTS_MISSING`;
- `assemble_review`;
- увесь порт `ResultBuilder` (`result_builders.py`).

Відрізняється:
1. **Позиції в коді.**
   - `Remark.position` (`models/review_structure.Position`, `kind="file"`) є в контракті, але `build_text_review` його не заповнює.
   - Найдешевше: `value="app/main.py:12–14"`. Асемблер ставить його у фразу `position.file: файл {name}` без змін, тож правки фразника у ~60 мовах не потрібні.
   - Для невиконаного критерію місця немає ні в рядку вердикту, ні у відповіді пояснення. Потрібне необов'язкове `place` у `verdict_explanation.CriterionRemark`. Код перевіряє, що файл є серед файлів студента й рядки в межах (як `drop_unshown_links` для посилань); невірне місце вирізається, повтору немає.
2. **Посилання на курс.** `CriterionRemark.links` вибрані й збережені, але в `Remark.read` збирач їх не перекладає. Це **спільна прогалина** з task/short_task, не лише проєкту.
3. **Різниця з базою.**
   - Факти коду (кількість змінених, нових і видалених файлів, версія бази, її застарілість) є в маніфестах.
   - Місце в структурі: `verification.by_reading`. Але це вільні рядки, і їх мову має давати фразник, а не модель.
   - Рекомендація: у кроці 2 не виводити. `verification.by_run` належить кроку 4.
4. Новий клас `ProjectResultBuilder` (або параметризований `TextResultBuilder`) у таблиці `get_result_builder` плюс перевірка конфігурації: «тип на `new_path` без збирача — помилка підйому».

## г. Служба запуску коду: що дають архіви й база

**Що є:**
- Знімок студента — **повний проєкт** (надмножина бази, `normalizer/models.py`, коментар до `_PROJECT_NORMALIZE_LIMITS`), канонічний zip у S3 (`HomeworkSubmission.snapshot_key`). Хеш кожного файла — в `snapshot_manifest`.
- База — `storage/orm.py::ProjectBase`: версії тільки додаються, є `archive_key` (оригінал для студента), `snapshot_key`, `snapshot_hash`, `manifest`, стан `pending → ready | failed`. Її готує `workers/base_normalize.py`.
- Тести в базі: код їх не вимагає й не розпізнає. Це дані автора. `tests/` не під забороною, тож у знімку тести є, якщо автор їх поклав.
- Файли залежностей: `requirements*.txt`, `pyproject.toml`, `package.json`, `Dockerfile`, `Makefile` у знімку лишаються. Їх розпізнає за іменем лише `ingestion/code_typicality.py`, і тільки для матеріалів курсу.
- Структура рецензії вже має `Verification.by_run` / `by_reading`.
- Критерії з `check_method: code_test` чекають на запуск: поки що їх судить модель (`soft_descent`).

**Чого немає:** профілю запуску (рантайм, встановлення, команда тестів, точка входу, ліміти) ні в `AuthoredDocument`, ні в `ProjectBase`. Немає й захисту тестів автора: студент може змінити `tests/` бази. `compute_delta` показав би це як `changed`, але ніхто цього не перевіряє.

**Пропозиція порту** (backend, `homework/code_run_port.py`; сервер — окремо):

```python
class CodeRunPort(Protocol):
    async def submit(self, request: RunRequest) -> RunTicket: ...   # швидко, без очікування
    async def result(self, run_id: str) -> RunResult | None: ...     # None — ще йде

@dataclass(frozen=True)
class RunRequest:
    run_id: str                  # ключ ідемпотентності = sha256(snapshot_hash, base_snapshot_hash, profile_hash, runner_version)
    submission_snapshot: ObjectRef   # S3-ключ + sha256 канонічного zip
    base_snapshot: ObjectRef | None
    protected_paths: tuple[str, ...] # файли бази, що перекривають студентові (тести автора)
    profile: RunProfile

@dataclass(frozen=True)
class RunProfile:                # на версію бази (тести міняються з базою)
    image: str                   # лише з білого списку, напр. "python:3.12"
    install: tuple[str, ...]     # argv; мережа — лише на встановлення, через дзеркало
    test: tuple[str, ...]        # argv, напр. ("pytest", "-q", "--junitxml=/out/junit.xml")
    entry: tuple[str, ...] | None
    limits: Limits               # секунди встановлення і тестів, cpu, пам'ять, диск, вихід логу

@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: Literal["completed", "install_failed", "timed_out", "refused", "infra_failed"]
    environment: Environment     # дайджест образу, версія рантайму, sha256 фіксованих залежностей
    install: StepResult          # exit_code, duration_ms, log_tail (обрізаний, позначено)
    tests: TestsResult | None    # підсумок + випадки: id (nodeid), file, line?, outcome, message≤2 KB, origin base|student
    entry: StepResult | None
    overridden_protected: tuple[str, ...]  # які файли автора студент змінив (і що їх перекрито)
```

Як вбудувати: кодова стадія `code_run` (`code: true`) подає запит і заморожує ревізію. Для цього потрібні `FreezeReason.AWAITING_RUN` і стан `awaiting_run`; міграція — лише коментар колонки, як `submission_awaiting_criteria`. Результат приходить підписаним зворотним викликом → `resume_after_code_run` → `start_continuation_job`. Запасний шлях — наявний `sweep_frozen_revisions`. Кожен запуск — рядок `ExternalServiceCall` (`action=code_run`), бо це платна зовнішня служба. Однаковий `run_id` для тієї самої роботи дає кеш, і це збігається з відбитком задачі 13.

## ґ. Розклад кроку (2) на коміти

| # | що змінюється | розмір | міграція | прод (project на сьогоднішньому) | хмара |
|---|---|---|---|---|---|
| K1 | `process_project_submission` повертає `DoorReading.files`: файли студента (`changed + new`) з `screen.texts`, `has_lines=False` для DOCUMENT. Сьогоднішнє тіло `files` не читає. Тести в `test_project_one_door.py` | S | ні | не змінює | так |
| K2 | Бюджет project для нового шляху — від стадій `submission_paths.yaml` (новий виклик поруч, старий не чіпати) | S | ні | не змінює | так |
| K3 | Правило «цитата студента»: `RepeatReason.QUOTE_FROM_BASE`; текст бази для змінених файлів іде поруч із `WorkFile` (нове поле чи окремий кортеж). Чисті функції й тести | M | **рішення:** окремий прапорець `quote_from_base` у рядку вердикту — міграція S. Зведення до `quote_not_found` перекручує зміст | не змінює | так |
| K4 | Промпти `prompts/criteria_evaluation_project/v1.md`, `prompts/review_explanation_project/v1.md`; стадії `project_criteria_evaluation` і `project_review_explanation` у YAML (стелі «не калібровано»), реєстрація виконавців; замки рендеру промптів | M | ні | не змінює (project лишається `todays_mentor`) | так |
| K5 | Місце в коді для невиконаного критерію: `CriterionRemark.place`, перевірка й вирізання; `Remark.position` і `Remark.read` у збирачі | M | ні (JSON у `SubmissionExplanation.body`) | не змінює прод project; **змінює вихід task/short_task** у тестовому режимі (позиції й посилання з'являться) | так |
| K6 | `ProjectResultBuilder` у `get_result_builder`; перевірка підйому «new_path без збирача — помилка» | S–M | ні | не змінює | так |
| K7 | Інтеграційний тест: project повним новим шляхом (рендер-дубль маршрутизатора, як у `test_one_door_db`): рамки, цитати з місцем, відмова цитати з бази, збирач | M | ні | не змінює | так |
| K8 | Перемикання: `project: served_by: new_path, test_mode: true`, шляхи `[safety, attempt_classifier, project_criteria_evaluation, course_link_lookup, project_review_explanation]` | S | ні | **змінює**: project іде новим шляхом, без списання | код — так; живий замір стель і рішення про злиття — оператор |

Порядок: K1 → K3 → K4 → K6/K5 → K7 → K8. K2 можна робити будь-коли до K8.

**Що з кроку (3) можна паралельно з (2):**
- (а) порт `CodeRunPort` з типами й фальшивим адаптером: нові файли, нічого з кроку 2 не чіпає;
- (б) профіль запуску на версію бази: `project_bases.run_profile` JSONB (міграція) і поле в API автора;
- (в) детерміноване виявлення тестів і файлів залежностей зі знімка за іменем (`tests/`, `test_*.py`, `*_test.py`, `requirements*.txt`, `pyproject.toml`): чиста функція над маніфестом;
- (г) сам сервер запуску в окремому репозиторії;
- (д) перевірка «студент змінив тест автора» за `compute_delta`.

Вбудовування стадії `code_run` у шлях project — після K8, бо торкається того самого переліку стадій.

## д. «Спроба» (задача 13) для project

**Є:**
- `HomeworkSubmission.snapshot_hash`: агрегат `шлях:хеш` очищеного знімка. Не залежить від перепакування zip, тому годиться як відбиток «тієї самої роботи».
- `snapshot_manifest`: хеш кожного файла; `snapshot.zip` у S3 — тексти для рядкової різниці.
- `normalizer.compute_delta(prev_manifest, cur_manifest)`: та сама функція, що й для бази, годиться для різниці з попередньою спробою.
- `path_selection.resolve_submission_state`: перша чи повторна, через `has_reviewed_revision` (той самий студент і задача, `completed/delivered`, не видалена).
- `SubmissionVerdictRepository.previous_verdicts` і запобіжник повторної подачі: останній попередній «met», чия цитата досі в роботі.
- Розділи `fixed/open/broken` у `ReviewStructureV1` є в контракті, але не заповнюються.
- Сьогоднішній Ментор має історію в `review_graph._history` на глибину 3 задачі.

**Бракує:**
- Номера спроби: колонки немає. Рахувати при читанні чи зберігати (міграція) — рішення. Треба також вирішити, які статуси рахуються: чи враховувати `rejected`/`mismatch`.
- Пошуку «тієї самої роботи» за `snapshot_hash`: `find_duplicate` на дверях API звіряє `file_hash` сирих байтів. Перепакований той самий проєкт його обходить, а `snapshot_hash` відомий лише у воркері, після нормалізації. Треба запит (студент, задача, `snapshot_hash`, `completed/delivered`) у `process_project_submission` і **індекс** на `homework_submissions.snapshot_hash` (його немає; міграція). Окреме рішення: що робити зі збігом (віддати попередню рецензію без оплати?).
- Посилання на попередню спробу: `previous_submission_id` чи вибірка на льоту. Якщо версія бази між спробами змінилась, різниць дві: з базою і з попередньою спробою. Їх треба тримати окремо.
- Спільного порту для всіх типів, наприклад `WorkFingerprint`: project → `snapshot_hash`; один файл → хеш NFC-тексту (а не `file_hash` сирих байтів?); тест → хеш канонічних відповідей. Нині кожен тип має своє і порту немає.

## Відкриті рішення для оператора

1. Правило цитати: C (рекомендую), B чи A. І чи потрібен окремий прапорець рядка (міграція).
2. Окремі стадії `project_*` з власними промптами (рекомендую) чи гілка `task_type` в одному промпті.
3. Чи показувати складанню списку дерево і тести бази (кеш за `content_hash` документа цього не врахує).
4. Різниця з базою в рецензії: зараз не виводити (рекомендую) чи `verification.by_reading` через фразник.
5. Профіль запуску: на версію бази (рекомендую) чи на задачу.
