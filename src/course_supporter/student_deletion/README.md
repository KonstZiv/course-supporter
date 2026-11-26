# Видалення облікового запису студента на запит (Б-2)

Умови й політика конфіденційності редакції 1.2 (чинні з 9.12.2026): студент просить
видалити обліковий запис — платформа протягом 30 днів видаляє логін, ім'я й пошту, а
роботи й рецензії знеособлює та зберігає. Запит приходить на пошту підтримки;
адміністратор запускає команду в контейнері застосунку.

Код: `service.py` поруч; команда — `scripts/student_admin.py`.

## Як запускати

На проді **спершу завжди `--dry-run`**. Справжній запуск — окреме рішення адміністратора.

```bash
docker exec -it course-supporter-app python -m scripts.student_admin delete \
    --tenant <id або назва> --student <id> --actor <хто> \
    --reason "<підстава без особистих даних, напр. номер звернення>" --dry-run
```

Той самий рядок без `--dry-run` видаляє. `--reason` із символом `@` — відмова: у підставу
пошту не пишемо.

**`--dry-run`** нічого не пише (транзакція READ ONLY, сховище й Redis — лише читання).
Він друкує:

- скільки рядків і яких полів зміниться в кожному місці; для старої назви файла — скільки
  входжень буде замінено в кожному полі (лише кількості, без самих назв; поле без
  входжень не друкується);
- скільки незавершених подач і активних резервів (з ними справжній запуск відмовить);
- по кожній чинній партії юнітів студента — невикористані юніти, сплачену суму, валюту
  й суму повернення: сума партії × невикористані ÷ юніти партії, ціле в мінорних
  одиницях, округлення вниз; подарункові (кредит адміністратора) і тестові партії —
  окремо, повернення 0;
- id інших студентів в **інших** орендарях із тією самою підтвердженою поштою (лише id —
  підтримка вирішує, чи видаляти й їх).

Жодних імен, пошт і логінів у виводі немає.

Приклад (числа з тесту `test_a_dry_run_writes_nothing_and_shows_refunds`):

```
student 01a2…  tenant 01a2…  dry run: nothing written
would change:
  student_credential_tokens          2 row(s)  [deleted]
  student_credentials                1 row(s)  [login, password_hash, is_active, recovery_email, recovery_email_confirmed_at]
  portal_signups                     2 row(s)  [deleted]
  student_enrollments                1 row(s)  [deleted]
  homework_submissions               1 row(s)  [student_note]
  remark_comments                    1 row(s)  [body (the student's replies)]
  student_feedback                   1 row(s)  [text]
  homework_submissions               2 row(s)  [original_filename, file_url (file renamed)]
  homework_submissions               1 occurrence(s)  [review_result: old file name replaced]
  homework_submissions               1 occurrence(s)  [review_markdown: old file name replaced]
  submission_criterion_verdicts      1 occurrence(s)  [quote_file: old file name replaced]
  external_service_calls             1 occurrence(s)  [input_text: old file name replaced]
  external_service_calls             1 occurrence(s)  [output_text: old file name replaced]
  students                           1 row(s)  [external_id, display_name, metadata, mentor_preferences, deleted_at]
  billing_accounts                   1 row(s)  [closed and anonymized]
  billing_payment_rules              1 row(s)  [student rules ended]
  storage                            2 row(s)  [objects renamed]
  redis                              2 row(s)  [rate-limit counters deleted]
in progress: 0 unfinished submission(s), 0 active reserve(s)
units (refund = amount paid x unused / batch units, rounded down; through the payment provider, by support):
  batch 01a2…  purchase unused 60/100  paid 500 EUR  refund 300 EUR (minor units)
  batch 01a2…  purchase unused 300/300  paid 2000 EUR  refund 2000 EUR (minor units)
  batch 01a2…  test     unused 20/20  test  refund 0
  batch 01a2…  gift     unused 50/50  gift (administrator's credit)  refund 0
  total refund 2300 EUR (minor units)
same confirmed e-mail in other tenants: none
```

Коди виходу: 0 — готово; 1 — відмова (текст каже чому; нічого не записано); 2 — помилка
в аргументах; 3 — видалення закомічене, але крок після коміту (сховище чи Redis) не
вдався й після повторів (див. рядок ERROR; для сховища — `finish-storage`).

## Відмови

Кожна — своїм текстом, нічого не змінюється:

- `student <id> not found`;
- `student <id> belongs to another tenant, not <tenant>`;
- `student <id> already deleted at <час> UTC` — і повторний запуск так само;
- `the reason holds '@'` (і порожня підстава);
- `has N unfinished submission(s) and M active reserve(s)` — незавершена подача — будь-який
  статус, крім `completed / delivered / rejected / mismatch / failed`; утримки
  (`awaiting_funds`, `awaiting_criteria`) теж: їх відновлює поповнення чи продовження, а
  відновлена перевірка потребує живого платника;
- копія файлу не вдалася або відрізняється від оригіналу (див. нижче).

## Що робить справжній запуск

Одна транзакція бази:

1. **Вхід вимкнено.** Логін → `deleted-<повний id студента>`, хеш пароля — непридатний (не
   підходить жоден пароль), обидві пошти → NULL, облікові дані вимкнено (`is_active`),
   токени відновлення й підтвердження видалено. Заявки запису на відкритий курс видалено:
   ту, з якої студент з'явився (з логіном, поштою, ім'ям і хешем пароля), і незавершені в
   цьому орендарі з його поштою. Сеанс порталу — токен без стану, який на кожному запиті
   звіряється з обліковими даними й `students.deleted_at`, тож старий сеанс одразу
   отримує 401.
2. **Рядок студента стає маркером:** `external_id` → `deleted-<id>` (повторна подача з тим
   самим ідентифікатором інтегратора створить нового студента, а не прив'яжеться до
   маркера), `display_name`, `metadata`, `mentor_preferences` → NULL, `deleted_at` = зараз.
   Мова інтерфейсу й рецензій, позначка «18+» і позначка тестового студента лишаються —
   вони нікого не ідентифікують.
3. **Записи на курси видалено** — як тоді, коли автор знімає студента з курсу (запис — це
   доступ, не історія; подачі прив'язані до завдання, а не до запису).
4. **Особисте поза роботою затерто:** коментар чи питання до подачі (`student_note`) → NULL;
   тексти реплік студента на зауваження → `[deleted at the student's request]` (відповіді
   Ментора лишаються); вільний текст відгуку → NULL (сама оцінка «допомогло / ні»
   лишається для статистики).
5. **Ім'я файлу роботи** (рішення власника 25.11, варіант A): ключ у сховищі мав вигляд
   `homework/<орендар>/<подача>/<ім'я файлу студента>`. Об'єкт копіюється на
   `homework/<орендар>/<подача>/submission.<розширення>` — вміст байт у байт той самий,
   копія звіряється з оригіналом за SHA-256 до коміту. Розширення береться з
   оригінального імені, якщо двері завантаження його приймають (`.tar.gz` — цілим);
   інакше — просто `submission`. Слідом переписуються `file_url` і `original_filename`.
6. **Стара назва файла — всюди, де вона лишилась** (рішення власника 26.11, `file_names.py`):
   у тій самій транзакції точне входження старої назви (повної, з розширенням) замінюється
   на `submission.<розширення>` у всіх текстових і JSON-полях рядків студента:
   - подачі: `review_result` (місце зауваження `назва:рядки`, `text_result._place_of`),
     `review_markdown` (рядок місця, `review_assembler._position_line`), `sanity_result`,
     `safety_result`, `snapshot_manifest`, `attempt_receipt`, `error_message`;
   - вердикти: `quote_file`, `quote`, `missing`; тіла пояснень; кандидати посилань;
     відповіді Ментора на репліки;
   - завдання подач (`jobs`: `input_params`, `result_data`, `stage_progress`,
     `error_message`) і їхні рядки реєстру (`external_service_calls`: `input_text`,
     `output_text`, `error_message`); оцінки вартості подач (`action_cost_estimates`).

   Структуроване значення (`quote_file`, місце `назва:рядки`) і вільний текст — одним
   правилом: назва ціла й окремо (не частина довшого слова: `a.py` у `data.py` не
   замінюється) і **не після `/` чи `\`** — шлях усередині архіву проєкту (`src/main.py`) —
   це вміст роботи, не назва завантаження, і лишається. У JSON переписуються ключі й
   рядкові значення. Одна заміна на всі подачі студента: пізніша рецензія може назвати
   файл попередньої спроби. М'яко видалене завдання, що містить назву, — відмова (тригер
   м'якого видалення не дасть його переписати).

   Цілісність: жоден код не звіряє хеш `review_result`, `review_markdown` чи `output_text`.
   `input_hash` реєстру лишається як був (хеш, назви не містить); інструменти вимірювання
   `scripts/record_explanation_input.py` / `record_evaluation_input.py` для подач видаленого
   студента покажуть `matched=false` — перебудований вхід уже з новою назвою.
   Ключі **без** імені студента не змінюються: відповіді тесту (`…/<uuid>/answers.json` —
   назва платформи), знімок проєкту (`…/<подача>/snapshot.zip`).
7. **Облік:** `anonymize_student_accounts` у тій самій транзакції — рахунки закрито й
   знеособлено, правила оплати про студента завершено (аудит пише вона сама: `account.anonymize`,
   `payment_rule.end`).

Якщо копія файлу не вдалася чи відрізняється — транзакція не комітиться, уже зроблені
копії (з нейтральними іменами) прибираються, код виходу 1.

Після коміту, з повтором:

- старі об'єкти (з іменем студента) видаляються. У журнал пишуться id подач, **не ключі**.
  Наявне прибирання сховища (`enqueue_s3_cleanup`) тут не підходить: воно записує ключі
  в `jobs.input_params`, тобто повернуло б ім'я файлу в базу. Невдача — ERROR
  `student_deletion_storage_failed` з id подач і код 3; тоді:

  ```bash
  docker exec -it course-supporter-app python -m scripts.student_admin finish-storage \
      --tenant <id або назва> --student <id> --actor <хто>
  ```

  Вона видаляє в теці кожної подачі все, крім її файлу й знімка проєкту.
- лічильники обмеження частоти в Redis, прив'язані до студента
  (`rl:login_by_email_email:<sha256 пошти>`, `rl:join_course_student:<sha256 id>`),
  видаляються. Невдача — ERROR `student_deletion_redis_failed` і код 3; лічильники й так
  зникають самі не пізніше ніж за годину.

Журнал: рядок `student_deleted` (actor, reason, tenant_id, student_id, кількості змін) —
без імені й пошти. Окремо в аудит білінгу команда не пише.

## Чого команда НЕ робить

- **Не повертає гроші.** Перед видаленням підтримка пропонує студентові повернення
  невикористаних юнітів через посередника (сума з `--dry-run`). Якщо студент відмовився —
  юніти лишаються на закритому рахунку й згасають наявним завданням `billing_expire` у
  кінці терміну дії партії.
- **Не змінює вміст файлів робіт** (рішення 1): ім'я, вписане студентом у сам файл,
  неможливо відрізнити від решти тексту. Шляхи файлів усередині архіву проєкту — теж
  вміст роботи.
- **Не видаляє подачі, вердикти, пояснення, рецензії й цитати з роботи.** На них
  тримаються статистика автора й облік; у них замінюється лише стара назва файла.
- **Не чіпає юридичних доказів:** згоди (`billing_consents`: рахунок, канал, версія умов,
  SHA-256 тексту — без IP і пристрою) і аудит білінгу (знімки без власника, непрозорі
  id); партії, оформлення, події посередника й повернення (id замовлень, суми; пошти
  покупця в базі немає).
- **Не чіпає журналів контейнерів:** рядок `homework_file_uploaded` під час подачі
  записав ключ з іменем файлу; журнали ротуються окремо. Лічильник частоти входу в
  портал у пам'яті процесу API (ключ з логіном, до 60 с) з команди недосяжний.
- Стара версія об'єкта в сховищі (якщо відро версіоноване) ховається й зникає за
  правилом життєвого циклу відра — до 29 днів, у межах обіцяних 30.

## Перелік місць (крок 1 Б-2)

| Місце | Клас | Дія |
|---|---|---|
| `students.external_id`, `display_name`, `metadata`, `mentor_preferences` | вхід і особа | `deleted-<id>` / NULL |
| `student_credentials`: login, password_hash, recovery_email (+ confirmed_at), is_active | вхід і особа | `deleted-<id>`, непридатний хеш, NULL, false |
| `student_credential_tokens` | вхід і особа | видалити |
| сеанси порталу (токен без стану) | вхід і особа | недійсні з комітом |
| `portal_signups` (підтверджена студента; незавершені з його поштою в орендарі) | вхід і особа | видалити |
| `student_enrollments` | доступ | видалити |
| `homework_submissions.student_note`, `remark_comments.body` (лише студента), `student_feedback.text` | особисте поза роботою | NULL / маркер |
| `homework_submissions.original_filename`, `file_url`, ключ об'єкта | особисте поза роботою (ім'я файлу) | нейтральне `submission.<розширення>`, об'єкт скопійовано байт у байт |
| стара назва файла в: `review_result`, `review_markdown`, `sanity_result`, `safety_result`, `snapshot_manifest`, `attempt_receipt`, `error_message`; вердиктах (`quote_file`, `quote`, `missing`); поясненнях; кандидатах посилань; відповідях Ментора; `jobs`; `external_service_calls` (`input_text`, `output_text`, `error_message`); `action_cost_estimates` | особисте поза роботою (ім'я файлу) | точна заміна назви на `submission.<розширення>` |
| решта тексту рецензій, вердиктів, пояснень, виходів стадій; шляхи всередині архіву; вміст файлів у сховищі | вміст роботи | без змін |
| згоди, аудит білінгу, партії, оформлення, події посередника, повернення | юридичний доказ | без змін |
| Redis `rl:…` за поштою й id студента | вхід і особа (псевдонім) | видалити після коміту |
| журнали контейнерів; лічильник входу в пам'яті API | поза базою | не чіпає (див. вище) |
