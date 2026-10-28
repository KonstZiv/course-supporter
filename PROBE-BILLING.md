# Зондування білінгу — факти з коду

Стан коду: `main` @ `0648350` (2026-10-28). Лише читання; код, тести й конфігурацію не змінено.
Позначки: **Факт** — з посиланням `файл:рядок`; **Припущення** — висновок, не перевірений запуском; **Не знайдено** — із зазначенням, де шукав.

## Коротко

1. **Порт коштів існує, але операцій п'ять, не три.** Три для подачі (`check_and_reserve`, `account_stage_cost`, `release_remainder`) і ще дві для «роботи раз на версію» — пояснень до ключа тесту (`check_and_reserve_for_version`, `account_version_work_cost`). Реалізація лише одна — `AlwaysEnoughFundsPort`: вона нічого не утримує, завжди дозволяє й пише рядок `funds_port` у реєстр (`src/course_supporter/funds_port.py:215-333`). Відмінність від брифу не суттєва (розширення, а не інша модель), тому роботу я не зупиняв.
2. **«Утримання» в коді — це статус подачі, не гроші.** `awaiting_funds` і `awaiting_criteria` у `homework_submissions.status` плюс checkpoint у `jobs.stage_progress`. Грошового резерву немає ніде: ні таблиці, ні Redis, ні пам'яті. Обидва утримання знімає запуск нової роботи (`homework/path_runner.py:184-194`).
3. **Сьогоднішній Ментор порт не кличе зовсім.** На новому шляху зараз лише тип `test`, а він не робить жодного платного виклику: оцінка дорівнює 0.0 (`config/submission_paths.yaml:217-222`). Отже в проді порт отримує реальні суми лише від пояснень ключа. Текстові типи й проєкти, а також увесь контур матеріалів (ingest, Methodist, підсумки вузлів) повз порт оплачують моделі без жодного питання.
4. **Із липневих дір закрита лише одна, і то частково, а з'явилась нова.** `cost_usd` досі `NULL` на невдалих викликах. Пропуск рядка без `job_id` досі в коді, але seam тепер ставить контекст усім обгорнутим робітникам. Витрати домашніх робіт прив'язані до курсу через `HomeworkSubmission`. **Нова діра:** кожна робота-продовження перезаписує `homework_submissions.job_id`, тож у звітах губляться витрати попередніх робіт тієї самої подачі.
5. **Оцінка стелі — сума `money_usd` стадій із `submission_paths.yaml`.** Прайс-каталог `external_services.yaml` вона не читає. Як верхня межа ризику вона придатна, а як ціна для платника — ні: вона не залежить від розміру подачі, усі чотири стелі стадій позначені «не калібровано», а DeepSeek у пікові години коштує вдвічі більше за ціну з каталогу.
6. **Повторно утримання знімає не seam, а нова робота.** ARQ сам перезапускає роботу після смерті робітника лише через приблизно 6 год (`job_timeout` + 10 с). Стартова перевірка осиротілих робіт до цього часу вважає їх живими. Подачу без checkpoint (усі подачі сьогоднішнього Ментора) повтор запускає з нуля, і вона платить удруге.
7. **Ідемпотентність доведеться будувати самому.** `check_and_reserve` кличеться на кожному запуску тієї самої подачі (після `Retry`, при продовженні, після відновлення). `release_remainder(COMPLETED)` теж може прийти двічі. На шляху аварійного провалу (`_fail_the_submission`) порт не чує нічого, тож утримання там витекло б. Природні ключі такі: `submission_id` (+ ім'я стадії) для подачі та (`authored_document_id`, `source_content_hash`) для версії.
8. **Платник — Tenant.** Окремої моделі автора немає. Студент прив'язаний до орендаря (`uq tenant_id, external_id`). Зарахування — `student_enrollments` (студент ↔ кореневий `CourseNode`). Поняття «відкритий курс» і квот на подачі не знайдено.
9. **Адміністрування ведеться лише через CLI.** `manage_tenant.py` має шість субкоманд на sync-сесії. Адміністративних маршрутів API немає. Вхідних вебхуків і перевірки підпису теж немає. Вихідні вебхуки (`reviewed` / `mismatch` / `failed`) не підписані.
10. **Міграції пишуться руками й мають рядкові id.** Голова alembic — `submission_awaiting_criteria`. Найдорожчі в плані BIL-B (журнал) і BIL-E (списання й повернення в життєвому циклі ARQ). Обидві оцінюю як L, бо їхня складність — в ідемпотентності й одночасності, а не в схемі.

---

## 1. Порт коштів

### Інтерфейс і реалізація
- **Факт.** Протокол `FundsPort` (структурний `Protocol`) і `AlwaysEnoughFundsPort` лежать в одному модулі: `src/course_supporter/funds_port.py:215-262` (протокол) і `:265-333` (реалізація).
- **Факт.** Контексти: `SubmissionContext(tenant_id, student_id, submission_id, path_key)` (`funds_port.py:117-134`) і `VersionWorkContext(tenant_id, authored_document_id, source_content_hash, work_kind)` (`:152-178`). Обидва frozen, kw_only.
- **Факт.** Суми передаються в доларах як `float` (`funds_port.py:40`). Відмова повертається значенням `FundsAnswer`, винятку не буває. Інваріант «причина є тоді й лише тоді, коли відмова» перевіряється в `__post_init__` (`:194-202`) і в CHECK `ck_esc_funds_refusal_reason` (`storage/orm.py:1215-1219`). Словник причин має одне значення — `INSUFFICIENT_FUNDS` (`funds_port.py:87-102`).

| Операція | Отримує | Повертає | Факт |
|---|---|---|---|
| `check_and_reserve` | `SubmissionContext`, `ceiling_estimate_usd: float` | `FundsAnswer` | `funds_port.py:218-225` |
| `account_stage_cost` | `SubmissionContext`, `stage_cost_usd: float` | `None` | `:227-231` |
| `release_remainder` | `SubmissionContext`, `SubmissionOutcome` (COMPLETED/FAILED) | `None` | `:233-237`, `:105-114` |
| `check_and_reserve_for_version` | `VersionWorkContext`, `ceiling_estimate_usd` | `FundsAnswer` | `:239-248` |
| `account_version_work_cost` | `VersionWorkContext`, `actual_usd` (фінальна операція) | `None` | `:250-262` |

- **Факт.** `AlwaysEnoughFundsPort.check_and_reserve*` завжди дозволяє й пише рядок реєстру через `record_funds_decision`. Інші операції порожні (`funds_port.py:282-333`).

### Хто й коли кличе
**Новий шлях** (`homework/path_runner.py`):
- **Факт.** Вхід — `run_new_path_if_switched`. Його кличе `arq_process_homework` першим кроком (`api/tasks.py:878-882`).
- **Факт.** `check_and_reserve` кличеться один раз за запуск: після «дверей» (безкоштовних Stage 1 / normalizer) і до першої платної стадії. Оцінка — `path_ceiling_estimate(config, choice.key)` (`path_runner.py:331-333`).
- **Факт.** `account_stage_cost` кличеться після кожної завершеної стадії, коли checkpoint уже збережено. Сума — `SUM(cost_usd)` з реєстру за `job_id` + `action = stage_name` (`path_runner.py:399-403`, `:626-648`).
- **Факт.** `release_remainder(COMPLETED)` кличеться в трьох випадках: коли стадія достроково закінчила шлях (`:404-407`), після `_finish` (`:444`) і `release_remainder(FAILED)` — коли вичерпано бюджет повторів (`:733-736`).
- **Факт.** На шляху винятку (`except Exception` → `_fail_the_submission`, `path_runner.py:238-241`, `:888-918`) порт **не кличеться**.
- **Факт.** Коли стадія «заморожена» (стелі, `awaiting_criteria`), порт свідомо не кличуть (`path_runner.py:757-765`, `:670-674`).
- **Факт.** Реалізація порту береться з параметра `funds_port`, а без нього — `AlwaysEnoughFundsPort` (`path_runner.py:153`, `:217-219`). `arq_process_homework` параметр не передає (`api/tasks.py:880`). На відміну від цього, `key_explain` читає `ctx.get("funds_port")` (`workers/key_explain.py:103`). Ніхто не кладе `funds_port` у `ctx` (**Не знайдено** у `worker.py`, `api/tasks.py`).
- **Факт.** На новому шляху сьогодні лише `test`, а його шляхи порожні (`config/submission_paths.yaml:217-222`). `short_task`, `task` і `project` мають `served_by: todays_mentor` (`:229`, `:236`, `:243`).

**Пояснення ключа тесту** (`workers/key_explain.py`):
- **Факт.** `check_and_reserve_for_version(context, KEY_EXPLANATION_CEILING_USD)` кличеться перед генерацією (`key_explain.py:132-141`). Відмова переводить версію у стан failed з причиною.
- **Факт.** `account_version_work_cost` кличеться і при невдачі, з уже витраченим (`:170-172`), і при успіху (`:191-192`).

**Сьогоднішній Ментор** (`api/tasks.py:884-1340`):
- **Не знайдено.** Викликів порту немає. Шукав `check_and_reserve|account_stage_cost|release_remainder|FundsPort` по `src/`: збіги лише в `path_runner.py` і `key_explain.py`.
- **Не знайдено.** Порту немає й у контурі матеріалів (`arq_ingest_material`, `arq_prepare_document`, `arq_regenerate_node_summary`, `base_normalize_task`). Шукав там само.

### Що таке «утримання»
- **Факт.** Це два статуси подачі: `AWAITING_FUNDS` і `AWAITING_CRITERIA` (`storage/orm.py:1367-1397`). До них додається заморожений checkpoint у `jobs.stage_progress` (`path_runner.py:775-799`, `:746-772`).
- **Факт.** Утримання ставлять двоє: `_hold_for_funds` — після відмови порту — і `_hold_at_stage` — коли стадія не може працювати (`awaiting_criteria`). Стелі грошей або виходу лишають статус `received` і записують лише checkpoint (`path_runner.py:101-110`).
- **Факт.** Знімає утримання запуск нової роботи: `run_new_path_if_switched` переводить подачу з `awaiting_*` у `received`, перш ніж знову питати порт (`path_runner.py:184-194`). Виправлення — коміт `2c34d95` («hold a revision for funds again instead of failing it»). Тепер повторна відмова дає перехід `received → awaiting_funds`, а не недозволений `awaiting_funds → awaiting_funds`.
- **Факт.** Роботу для продовження після поповнення має створювати `resume_after_top_up`. Поки що її ніхто не кличе (`homework/path_continuation.py:143-170`).
- **Факт.** `sweep_frozen_revisions` навмисно не чіпає `AWAITING_FUNDS` (`path_continuation.py:73-82`).
- **Не знайдено.** Грошового утримання немає: ні таблиці (шукав `class ` в `storage/orm.py`), ні ключів Redis, ні стану в пам'яті.

## 2. Облік витрат — `external_service_calls`

**Колонки, корисні для білінгу** (`storage/orm.py:1201-1360`):

| Колонка | Зміст | Факт |
|---|---|---|
| `job_id` (NOT NULL, FK) | єдиний обов'язковий зв'язок; орендар, курс і подача — через `jobs` | `orm.py:1226-1233` |
| `action` | ім'я стадії або сходинки ladder; `funds_port`, `review_metrics`, `webhook_*` | `orm.py:1234` |
| `provider`, `model_id` | NULL — рядок без виклику | `orm.py:1236-1245` |
| `unit_type`, `unit_in`, `unit_out`, `unit_out_reasoning` | токени або секунди | `orm.py:1271-1291` |
| `cost_usd` (**Float**, nullable) | вартість за каталогом | `orm.py:1293` |
| `success`, `outcome`, `finish_reason`, `skip_reason` | результат виклику | `orm.py:1297-1320` |
| `ceiling_estimate_usd`, `funds_decision`, `funds_refusal_reason`, `path_key` | рядок порту | `orm.py:1331-1354` |
| `created_at` | час | `orm.py:1355` |

- **Факт.** Колонок `tenant_id`, `submission_id` і `course_node_id` у реєстрі немає: KD5 залишив лише `job_id` (`orm.py:1229-1232`, `storage/repositories.py:186-188`). `jobs` має `tenant_id` (nullable), `course_node_id` (nullable), `subject_type`/`subject_id` і `stage_progress` (`orm.py:1066-1100`).
- **Факт.** Рядок порту не несе `submission_id`. Подачу знаходять через `jobs.subject_id` (`service_logging.py:346-380`).

**Липневі діри:**
1. **`cost_usd = NULL`, і фільтр `IS NOT NULL` недораховує — діра жива.**
   - **Факт.** `StageRouter` рахує вартість лише тоді, коли відповідь є й має токени (`llm/stage_router.py:952-967`). Невдалий виклик (`response is None`) записує `cost_usd = NULL` навіть тоді, коли провайдер міг узяти гроші (timeout після обробки).
   - **Факт.** Усі звіти фільтрують `cost_usd IS NOT NULL` (`storage/repositories.py:192-195`, `:225`, `:577`).
   - **Факт.** Рядки STT без тривалості аудіо теж мають NULL (`stt/router.py:237-242`). Невдалий STT пишеться з `provider="unknown"` (`service_logging.py:436-445`).
   - **Факт, пом'якшення:** стартова перевірка відмовляє запускатися, якщо в сходинки ladder немає ціни (`llm/stage_router.py:312`, `llm/rung_registry_check.py:110`).
2. **Пропуск запису без `job_id` — код живий, вхід звужено.**
   - **Факт.** `_persist` пропускає рядок, залишаючи warning і лічильник у пам'яті (`service_logging.py:252-264`). Помилку БД теж «ковтає» (`:296-310`).
   - **Факт.** Seam тепер ставить контекст `job_id`/`tenant` кожному обгорнутому завданню (`jobs/execution_seam.py:291-292`).
   - **Припущення.** Платні виклики поза seam сьогодні не трапляються: у маршрутах API `StageRouter` використовують лише для `validate_scope`, без LLM (`api/routes/node_summaries.py:155`). Лічильник пропусків, однак, живе тільки в пам'яті процесу (`service_logging.py:165-196`), тому для білінгового класу він не годиться.
   - **Факт, побічне:** `webhook._record_attempt` пише `ExternalServiceCall(job_id=get_current_job_id())` повз `_persist`, без перевірки на None (`homework/webhook.py:276-298`).
3. **Домашні роботи без прив'язки до курсу — у звітах закрито обхідним шляхом; з'явилась нова діра.**
   - **Факт.** Домашня робота створюється без `course_node_id` (`enqueue.py:237-246`). `HomeworkCostRepository` прив'язує витрати через `JOIN homework_submissions ON esc.job_id = hs.job_id` (`storage/repositories.py:520-580`).
   - **Факт, нова діра:** `create_homework_job` кожного разу кличе `set_job_id(submission_id, job.id)` (`enqueue.py:249`, `storage/homework_repository.py:537-549`), зокрема для робіт-продовжень (`path_continuation.py:128-130`). Тому JOIN бачить лише останню роботу подачі: витрати попередніх (до заморожування, до відмови) випадають зі звітів «домашніх». Для обліку за подачею потрібен зв'язок через `jobs.subject_id`.
   - **Факт.** Пояснення ключа (`enqueue_key_explanation`) теж не ставить `course_node_id` (`enqueue.py:471-476`), тож у `/cost/summary` вони потрапляють у `unattributed`.

**Як записується оцінка стелі**
- **Факт.** `record_funds_decision` пише один рядок `action='funds_port'` з `ceiling_estimate_usd`, `funds_decision`, `funds_refusal_reason` і `path_key` (текст `task/first`; NULL для версії). `provider`, `model_id`, `success`, `cost_usd` і `outcome` при цьому NULL (`service_logging.py:346-380`). Запис підкоряється тим самим правилам, що й `_persist`.

**Точність**
- **Факт.** `cost_usd` має тип `Float`, а не `Numeric` (`orm.py:1293`).
- **Факт.** Ціна DeepSeek у каталозі — нижня межа: у пікові години (01–04, 06–10 UTC, пн–пт) вона вдвічі вища (`config/external_services.yaml:232-235`). Знижки за кеш-попадання не враховуються: у моделі ціни лише `cost_per_1k_in/out` (`llm/registry.py:111-122`).

## 3. Прайс-каталог і оцінка стелі

- **Факт.** Будова `config/external_services.yaml`: `providers.<name>.{type: llm|stt|transcription, models: [{id, capabilities, max_context, max_output_tokens, unit_type: tokens|minutes, cost_per_1k_in, cost_per_1k_out | cost_per_minute}]}`, далі `strategies` і `actions.transcribe` (`config/external_services.yaml:46-527`). Історії цін немає: зміни записані лише в коментарях журналу ревізій (`:8-30`). Застарілі моделі лишаються «для ціни старих рядків» (`:236-240`).
- **Факт.** Вартість рахує `estimate_cost(tokens_in, tokens_out)`. Якщо ціни немає, повертається `None` (`llm/registry.py:111-122`). Для STT: тривалість / 60 × `cost_per_minute` (`stt/router.py:237-242`).
- **Факт.** Оцінка стелі шляху — `path_ceiling_estimate`, сума `ceilings.money_usd` стадій шляху з `config/submission_paths.yaml` (`homework/path_config.py:397-413`). Значення: `safety` 0.05, `attempt_classifier` 0.05, `criteria_evaluation` 0.55, `review_explanation` 0.18 (`config/submission_paths.yaml:48`, `:84`, `:140`, `:196`). Усі чотири в коментарях позначені «не калібровано» (`:38`, `:78`, `:122`, `:178`).
- **Факт.** `money_usd` — бюджет усієї ladder стадії. Router віднімає від нього оцінку кожної викликаної сходинки (`config/submission_paths.yaml:43-47`, `llm/stage_router.py::execute_stage`).
- **Факт.** Продовжений запуск подає оцінку повного шляху, а не залишку (`path_runner.py:331-333`).
- **Придатність для BIL-D (Припущення).** Як верхня межа на утримання вона годиться: детермінована, відома до старту, не залежить від входу. Як ціна для платника — ні:
  - вона в доларах, а ціна має бути в кредитах;
  - вона не залежить від обсягу подачі;
  - вона змінюється з кожною правкою YAML без версії;
  - ціни каталогу в неї не входять (лише вручну підібрані стелі).

  Формулу ціни варто зробити окремою чистою функцією (тип, стан подачі, розмір входу → кредити) з версією, а `path_ceiling_estimate` лишити як запобіжник маржі.

## 4. Життєвий цикл робіт ARQ

- **Факт.** Робітників два: `WorkerSettings` (типова черга; ingest, prepare, node summary, s3_cleanup, email, base_normalize, explain_key) і `HomeworkWorkerSettings` (черга `homework`; лише `arq_process_homework`) (`worker.py:245-317`). Обидва беруть `max_jobs = worker_max_jobs = 1`, `max_tries = 3`, `job_timeout = 21600` (`config.py:123-125`).
- **Факт.** Статусом `Job` володіє лише seam (`jobs/execution_seam.py`):
  - **active** — після перевірок на вході (немає рядка → `Retry`; at-rest → тихий вихід; мертвий суб'єкт → `obsolete`). `active → active` при повторі не змінює нічого (`:241-281`).
  - **complete** — коли тіло нормально повертає значення (`:352-355`).
  - **failed** — на будь-який виняток, без повторного підняття (`:324-349`). Так само на `Retry` в останній спробі (`:297-323`).
  - Термінальний запис іде в окремій сесії й «терпимий»: недозволений перехід пропускається (`:358-393`).
  - `on_terminal` викликається лише тоді, коли термінальний запис справді ліг (`:396-412`).
- **Факт, повтори.** `arq.Retry` проходить seam наскрізь (`execution_seam.py:297-323`). Новий шлях сам ставить себе в чергу до `submission_path_max_retries = 2` і стежить за `job_try` (`path_runner.py:719-731`, `config.py:132`).
- **Факт, ARQ.** Після смерті робітника ключ `in-progress` живе `job_timeout + 10` с (`.venv/.../arq/worker.py:273`, `:461`). Лише після цього ARQ знову бере роботу, з `job_try + 1`.
- **Відновлення осиротілих — де зараз.** **Факт:** `_reconcile_orphaned_in_flight_jobs` запускається один раз на старті **кожного** робітника (`worker.py:49-148`, `:207-212`):
  - перевіряє живість у ARQ в обох чергах;
  - неживу роботу з checkpoint знову відправляє в чергу — `resume_orphaned_path_job` (`path_continuation.py:342-369`);
  - решту переводить у `failed`.

  Поруч працює `sweep_frozen_revisions` (`worker.py:218-225`).
- **Припущення.** Поки ключ `in-progress` живий (до ~6 год), стартова перевірка вважає роботу живою й не чіпає її. Справжнє відновлення робить пізніший перезапуск ARQ.
- **Куди ставити списання й повернення (Припущення).**
  - **Списання при старті** — у реалізацію `check_and_reserve` (і `_for_version`): це вже «перед першим платним викликом». Для сьогоднішнього Ментора потрібна точка в `api/tasks.py` після «дверей» і перед Stage 2. Це правка тіла, яке канон «не чіпає», тому варто вирішити, чи білінг узагалі чекає перемикання типів на новий шлях.
  - **Повернення при збої:**
    1. `release_remainder(FAILED)` на шляху `_fail_the_submission` — зараз його там немає.
    2. Seam-рівневий `on_terminal(failed)` для робіт без порту — він уже існує як механізм (`execution_seam.py:49-52`).
    3. Робота, яку відновлення перевело у `failed` (`worker.py:138-140`), — тут потрібен хук або окремий прохід «рахунок закритих робіт без остаточного розрахунку».
- **Що зробить їх ідемпотентними (Припущення).** Підходять унікальні ключі в журналі:
  - `(submission_id, 'reserve')`;
  - `(submission_id, stage_name, 'debit')`;
  - `(submission_id, 'release')`;
  - для версії — `(authored_document_id, source_content_hash, kind)`.

  Запис робиться `INSERT … ON CONFLICT DO NOTHING` в одній транзакції з рухом балансу. `job_id` не годиться: продовження — це нова робота.

  Поточна послідовність `save_checkpoint` → `account_stage_cost` (`path_runner.py:399-403`) не атомарна. Якщо робітник помре між ними, стадія вважатиметься виконаною, а її вартість не буде врахована, і при відновленні її не врахують теж. Списання стадії має йти в одній транзакції з checkpoint або виконуватися при відновленні за реєстром.

## 5. Хто платник

- **Факт.** `Tenant`: `id`, `name` (unique), `is_active`, `webhook_url` (`storage/orm.py:159-193`). Полів білінгу немає.
- **Не знайдено.** Моделі «автора» окремо від `Tenant` немає (шукав `class .*Author`, `author` у `storage/orm.py`). Автором фактично є орендар: `CourseNode.tenant_id` (`orm.py:277`).
- **Факт.** `APIKey`: `tenant_id`, `key_hash` (SHA-256), `scopes` (`prep`/`check`), `plan_id` → ліміти частоти з `config/auth.yaml` (`orm.py:195-245`, `config/auth.yaml:22-33`). План стосується частоти запитів, а не грошей.
- **Факт.** `Student` належить орендарю: унікальність `(tenant_id, external_id)` (`orm.py:1400-1416`). Вхід на портал — `StudentCredential`, 1:1 (`orm.py:1785-1830`).
- **Факт.** `StudentEnrollment` (студент ↔ кореневий `CourseNode`) — без історії й без м'якого видалення (`orm.py:1953-1998`). Керують ним маршрути `/students/enrollments` під scope PREP (`api/routes/students.py:1-5`).
- **Факт.** `HomeworkSubmission` має `tenant_id`, `student_id`, `course_node_id` (корінь курсу), `node_id`, `authored_document_id` і `job_id` (`orm.py:1487-1510`, `:1610`).
- **Не знайдено.** «Відкритого курсу» і квот на подачі немає. Шукав `quota|open|is_public|visibility|self_enroll|max_submissions` у `storage/orm.py`, `config/`, `src/`. `/portal/submission-policy` повертає лише формати й розміри файлів (`api/routes/portal_config.py:73-90`).
- **Де жити балансу студента (Припущення).** Студент існує лише всередині орендаря, тому баланс «студента взагалі» сьогодні нема до чого прив'язати.
  - Найменш інвазивно: `billing_account` із полем власника-платника — `tenant_id` або `student_id`, з CHECK «рівно одне».
  - Порт уже отримує обидва id в `SubmissionContext`, тож вибір платника (автор / студент за політикою курсу) стає логікою адаптера, а не зміною шляху.
  - Політику «хто платить на цьому курсі» природно класти на кореневий `CourseNode` або на окрему таблицю політик курсу.

## 6. Адміністрування

- **Факт.** Субкоманди `scripts/manage_tenant.py`: `create-tenant`, `create-key`, `list-tenants`, `list-keys`, `revoke-key`, `deactivate-tenant` (`scripts/manage_tenant.py:198-246`). Працює на sync-сесії `create_engine(settings.database_url)` (`:31-38`) і пише в `print`. Для CLI це прийнятний виняток, але структурованого журналу аудиту немає.
- **Не знайдено.** Адміністративних маршрутів API немає. Шукав `admin` у `src/course_supporter/api/`. Знайшлися лише керування студентами «tenant-admin» під PREP (`api/routes/students.py:1-5`) і прихований `?no_cache` у `/cost` (`api/routes/cost.py:9-11`). Окремий admin-scope відкладено (`api/routes/cost.py:147`).

## 7. Вебхуки

- **Факт.** Вихідні вебхуки: `reviewed`, `mismatch`, `failed` на `tenant.webhook_url` або URL подачі (`homework/webhook.py:1-9`, `:50-68`). Повтори — inline exponential backoff. Заголовок лише `Content-Type`, підпису немає (`:153-201`). Кожна спроба записується в `external_service_calls` (`:276-298`).
- **Не знайдено.** Вхідних маршрутів вебхуків і перевірки підпису (HMAC, `compare_digest`) немає. Шукав `hmac|signature|compare_digest|webhook_secret` по `src/`. Найближчий зразок — хеш SHA-256 ключів API (`auth/keys.py:21-37`). Для Paddle потрібні новий маршрут, секрет у `Settings` (SecretStr), перевірка `Paddle-Signature` і таблиця оброблених `event_id` для ідемпотентності.

## 8. Міграції

- **Факт.** Голова alembic — `submission_awaiting_criteria` (`migrations/versions/submission_awaiting_criteria.py:28-29`; `uv run alembic heads` → `submission_awaiting_criteria (head)`). Її батько — `submission_verdict_tables`.
- **Факт.** Міграції пишуться руками, з рядковими id замість хешів: «Hand-written, not autogenerated (DD-L4-A: autogenerate on this schema emits ~200 operations of whole-schema comment and index drift)» (`migrations/versions/submission_awaiting_criteria.py:19-21`). Значення в коді міграції записані буквально, а не прочитані з ORM (`:35-36`). У `Makefile` досі є `migrate` = autogenerate (`Makefile:38-39`); CLAUDE.md радить те саме, але практика репозиторію інша.

## 9. Задачі BIL-A…G

| Задача | Що досі слушне | Що змінилось | Що поправити в задумі | Зусилля |
|---|---|---|---|---|
| **BIL-A** `billing_account` 1:1 до Tenant | Платник за замовчуванням — орендар; `Tenant` полів білінгу не має | Порт знаходить платника через `tenant_id` (`funds_port.py:59-61`) і вже має `student_id` | Передбачити власника-студента (CHECK «рівно одне з tenant_id/student_id») або явно відкласти; клас A/B/C і податковий статус — enum з CHECK; міграція руками | S–M |
| **BIL-B** партійний журнал | Незмінний; партія = джерело, термін, порядок | Потрібні операції «утримання/резерв» і «звільнення», бо порт має reserve/release, а не лише debit | Журнал проводок (reserve, debit, release, refund, expire) з ключами ідемпотентності за `submission_id`/версією; рух по партіях FIFO за терміном; блокування рахунку (`SELECT … FOR UPDATE`); `Numeric`, не Float | L |
| **BIL-C** облік витрат білінгового класу | Усі три діри актуальні (діра 3 — частково) | Порт отримує вартість стадії з реєстру (`path_runner.py:626-648`), тож точність реєстру стає точністю білінгу | Додати 4-ту діру (`homework_submissions.job_id` перезаписується, `enqueue.py:249`); вартість невдалих викликів; піковий тариф DeepSeek; `Numeric`; таблиця цін з `valid_from` і зв'язок рядка з версією ціни; тривкий лічильник пропусків | M–L |
| **BIL-D** формула ціни до запуску | Чиста функція — у дусі коду (`path_ceiling_estimate` уже чиста) | Стеля вже оголошується до старту й пишеться в реєстр | Розділити: ціна в кредитах (версіонована формула) ≠ стеля в доларах (запобіжник маржі); вирішити, що бачить платник при продовженні (повна ціна чи залишок) | S–M |
| **BIL-E** списання при старті / повернення при збої | Точка «при старті» вже є — `check_and_reserve` | Не правка тіл ARQ, а адаптер порту плюс додаткові виклики: `release_remainder(FAILED)` у `_fail_the_submission`; збій під час відновлення (`worker.py:138-140`); сьогоднішній Ментор без порту | Ідемпотентність за доменними ключами, не за `job_id`; атомарність checkpoint + списання; розрахунок для робіт, які закрило відновлення; вирішити долю сьогоднішнього Ментора та контуру матеріалів | L |
| **BIL-F** зарахування партії в `manage_tenant.py` | Файл і стиль argparse ті самі | — | Субкоманда `grant-credits --tenant --amount --source --expires`; після зарахування — `resume_after_top_up` для `awaiting_funds` (потрібні ARQ/Redis з CLI, або прохід на старті робітника); писати через structlog | S |
| **BIL-G** фіскальний порт із порожнім адаптером | Зразок готовий — `FundsPort` + `AlwaysEnough…` (Protocol + перша реалізація + тести) | — | Тримати окремо від порту коштів: фіскальне стосується грошей на краю (Paddle як MoR), а не кредитів | S |

## 10. Ризики

**Подвійне списання.**
- **Факт.** `check_and_reserve` повторюється на кожному запуску тієї самої подачі: після `arq.Retry`, після продовження (`awaiting_criteria`, стелі, поповнення) і після відновлення (`path_runner.py:331`; `path_continuation.py:85-140`, `:342-369`).
- **Факт.** `release_remainder(COMPLETED)` може прийти вдруге, якщо робота повторно входить у `completed` (`path_runner.py:121-130`, `:444`).
- **Припущення.** Повтор ARQ після смерті робітника запускає сьогоднішнього Ментора з нуля, і платні виклики повторюються.
- **Захист:** ключі ідемпотентності в журналі за `submission_id` (+ стадія), не за `job_id`.

**Витік кредитів при смерті робітника.**
- **Факт.** `_fail_the_submission` не кличе порт (`path_runner.py:888-918`).
- **Факт.** Робота без checkpoint, яку відновлення перевело у `failed`, оминає тіло й порт (`worker.py:126-140`).
- **Факт.** Між `save_checkpoint` і `account_stage_cost` є вікно (`path_runner.py:399-403`).
- **Факт.** Утримання не знімається, якщо подачу м'яко видалили в стані `awaiting_funds`: seam переведе роботу в `obsolete` і не викличе тіло (`execution_seam.py:261-279`).
- **Факт.** Скасування роботи (`jobs/cancellation_service.py`) — ще одна термінальна подія без порту.
- **Захист:** прохід-«прибиральник», що закриває резерви, чия подача або робота в термінальному стані, плюс строк життя резерву.

**Одночасна робота двох робітників.**
- **Факт.** Перевірка осиротілих робіт запускається на старті кожного з двох робітників (`worker.py:207-212`). `dispatch_homework` ставить у чергу без детермінованого `_job_id` (`enqueue.py:274-279`).
- **Припущення.** Якщо обидва робітники стартують одночасно, `resume_orphaned_path_job` може двічі поставити ту саму `Job` у чергу. Тоді два тіла працюють над однією подачею паралельно, бо seam не має блокування й пропускає повторний `active`. Це дає подвійний `check_and_reserve` і подвійні стадії.
- **Факт.** Від двох одночасних продовжень захищає частковий унікальний індекс `uq_jobs_subject_in_flight` (`orm.py:1053-1061`). Перевірка перед вставкою (`path_continuation.py:109-124`) — не блокування, але на конфлікті впаде INSERT, тож дубля не буде.
- **Захист:**
  - `_job_id=str(job_id)` при відправленні в чергу (ARQ відкидає дубль);
  - advisory lock на час перевірки осиротілих;
  - рух балансу під `SELECT … FOR UPDATE` рахунку або атомарний `UPDATE … WHERE balance >= :x`;
  - «код має бути готовий до кількох робітників» — сьогодні це забезпечують лише унікальний індекс і `max_jobs = 1`.
