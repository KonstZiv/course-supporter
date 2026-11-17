# Білінг — частини, правила списання, маршрути

Модуль веде **журнал юнітів** (рахунки, партії, рухи, резерви) і ставить його на шлях кожної платної
дії: перевірки роботи студента, обробки матеріалу автора, підготовки завдання. Задачі BIL-A…BIL-E,
BIL-E2, BIL-TESTMARK; рішення оператора — у docstring кожного модуля.

## Частини

| Модуль | Що робить |
|---|---|
| `accounts.py` | Рахунки: створення першою дією, що змінює стан (запис `account.create`), знеособлення |
| `payer.py` | **Єдине** місце, що вирішує, хто платить: правило для студента → правило курсу → «сам»; `resolve_payer` для дії, `find_payer` для читання |
| `ledger.py`, `journal.py` | Журнал: купівля, резерв, списання факту, звільнення, різниця |
| `charging.py` | Ціна P, поріг, резерв, факт — усе в юнітах; перемикач `BILLING_CHARGING_ENABLED` |
| `funds.py` | Порт коштів журналу (`LedgerFundsPort`) і ворота обробки та підготовки |
| `estimates.py` | E (очікувана вартість) у пам'яті; збій оцінки перевірки — `None`: діє стеля |
| `quotes.py` | Ціна перед дією для екранів і воріт подачі (`review_quote`, `review_price`, `author_quote`) |
| `held.py` | Утримані перевірки: хто платить, скільки бракує, які чекають автора |
| `resume_funded.py` | Продовжує утримані перевірки, щойно платник може заплатити |
| `outlook.py` | Нагадування: на скільки перевірок / днів вистачить юнітів |
| `links.py` | Зв'язок «рух журналу → курс → завдання» — один для `spending.py` і `costs.py` |
| `spending.py` | Витрати автора в юнітах по курсах і завданнях (без доларів) |
| `costs.py` | Звіт адміністратора: собівартість у доларах поруч із виставленими юнітами |
| `testmark.py` | Позначка тестових купівель і зняття їхніх вільних юнітів (BIL-TESTMARK) |

## Правила списання (`charging.py`)

* **Ціна** `P = ceil(E × (1 + margin) × K)`; **стеля** `N = ceil(стеля шляху USD × K)` — без запасу,
  та сама величина, що обмежує резерв (`ceiling_units`).
* **Поріг** `available ≥ P` (`threshold_applies`): студент за власну перевірку; автор без кредиту — за
  власну дію **і за перевірку студента за правилом оплати** (BIL-E2, 5а п. 2: платформа в борг не
  працює). Автор із `credit_allowed` — без порогу. Підготовка, яку запустила подача студента, —
  без порогу.
* **Тест** (вид `test`) безкоштовний, поки баланс платника (разом із зарезервованим) > 0 — для
  студента і для автора за правилом; автор із кредитом — завжди.
* **Резерв** на старті — `min(2E, стеля, доступне)`; **факт** — вартість викликів реєстру. Платник без
  кредиту платить не більше резерву, перевитрату бере платформа (`platform_covered`) — це не борг.
  Розпочата дія доходить до кінця.
* **Оцінка впала на стелю** (BIL-E2, 4а): будь-який збій — вибір шляху, читання файла (крім відмови
  дверей), типовий розмір, сама оцінка, не задано K — на екрані ціна N («не більше N»). Поріг
  (рішення оператора 15.11) — **типова ціна завдання** (як для `basis: "typical"`: медіана розміру з
  історії завдання або `typical_submission.chars`, із запасом margin); не вдалося й її — `available > 0`.
  Резерв — `min(N, доступне)`, списується факт, перевитрату понад резерв бере платформа. Одна функція
  `quotes.ceiling_threshold` — для воріт подачі, порту коштів на старті прогону й продовження
  утриманих; автор за правилом — те саме правило з його юнітами.
  Журнал — по одному попередженню `billing_quote_ceiling` на кожну невдалу оцінку, з `reason`
  (`path_choice_failed` | `file_read_failed` | `typical_size_failed` | `estimate_failed` |
  `rate_not_set`) і класом помилки, без тексту роботи. Без K усе коштує 0, і N теж 0.

Поки перемикач вимкнений, `build_funds_port` дає `AlwaysEnoughFundsPort`: нічого не резервується й не
утримується.

## Коли з'являється рахунок (`accounts.py`, BIL-E3)

Рахунок створює **перша дія, що змінює стан**: згода, оформлення купівлі, позначка, налаштування,
правило чи нарахування адміністратора, старт платної дії (`resolve_payer` / `charging.payer_of`).
У тій самій транзакції пишеться один запис `billing_audit_log`: `action = "account.create"`,
`entity = "account"`, `before = null`, `after = audit.account_snapshot` (без власника); `actor` —
`--actor` адміністратора, для дії користувача чи системи — `audit.SYSTEM_ACTOR = "system"`. Запис
пише лише виклик, чия вставка повернула рядок (`RETURNING`): дві одночасні перші дії дають один
рахунок і один запис.

**Читання рахунку не створює:** GET маршрути білінгу, маршрути оцінки, нагадування, списки порталу
з утриманими перевірками й команда `get-settings` читають через `accounts.find_active_account` і
`payer.find_payer` / `charging.find_payer_of`. Без рахунку відповідь — як для порожнього: усі складники
залишку 0, виписка порожня, `expiring_soon = null`, `consent_required = true`, нагадування — за
правилами порожнього рахунку, `purchase` — як для непозначеного.

## Утримана перевірка (`held.py`, `resume_funded.py`)

Відмова порту на старті прогону — стан `awaiting_funds` (файл, контрольна точка й місце
зберігаються). Чиїх юнітів він чекає, каже `charging.payer_of` — одне джерело правди для прогону,
продовження, порталу й панелі автора:

* студент платить сам — код порталу `awaiting_funds`;
* автор за правилом — код порталу `awaiting_author_funds`, і лічильник `held_reviews` у
  `GET /billing/outlook` автора.

`resume_funded` продовжує найстаріші першими: автор із кредитом — одразу; інакше доступне ≥ P з рядка
оцінки, а без рядка — поріг стелі `ceiling_threshold` (типова ціна, інакше доступне > 0); тест —
баланс > 0.

## Тестові купівлі (`testmark.py`, BIL-TESTMARK)

Поки посередник у тестовому режимі, тестова картка платить, а зараховується справжня партія
юнітів. Рішення оператора 16.11: такі партії позначені в базі й знімаються однією командою
адміністратора — перед увімкненням списання (`BILLING_CHARGING_ENABLED`).

* **Позначка** — таблиця `billing_test_batches` (`batch_id`, `marked_at`, `marked_by`), лише
  дописування (тригер `billing_refuse_update`; DELETE — лише п'ятирічне знищення, разом із партією).
  `marked_by = credit`: купівлю зараховано, коли посередник, що перевірив підпис події, був у
  тестовому режимі (`PaymentProvider.test_mode`) — позначка в тій самій транзакції, що й партія
  (`payments/webhooks.py`); у бойовому режимі позначки немає. `marked_by = backfill`: міграція
  `biltestmark_test_batches` позначила всі купівлі Creem, зроблені до неї (бойових ключів Creem на
  проді не було). Рахунок-фактура B2B і нарахування адміністратора не позначаються.
* **Зняття** — вільні юніти (`journal.available`) кожної позначеної партії виходять одним рухом
  `test_voided` (входить в `OUTFLOW_KINDS`: баланс зменшується). Зарезервовані й утримані
  (повернення, спір) юніти лишаються — команда їх друкує; наступний запуск забере те, що звільнилось.
  Повтор без вільних юнітів нічого не змінює. Аудит — `batch.test_void` на партію, з якої щось
  знято: `before` / `after` = `{"free_units": n}` (до й після, після — 0).
* **Виписка** — рядок `test_voided`: `affects_balance = true`, юніти зі знаком мінус.

```bash
uv run python scripts/billing_admin.py void-test-batches --actor <ім'я> [--account <id>] [--dry-run]
uv run python scripts/billing_admin.py show-account --account <id>   # рядок «test units=N»
```

`--dry-run` — транзакція READ ONLY: той самий вивід, нічого не записано. Вивід — рахунок (id і
вид, без ідентифікаторів власника), партія, `voided`, `left reserved`, `left held`; у кінці підсумок.

## Macropay — другий посередник (`payments/macropay.py`)

Запасний шлях, якщо Creem відмовить або затягне перевірку. На проді вимкнений: `BILLING_PROVIDERS`
лишається `["creem"]`. Написаний за документацією docs.macropay.ai (прочитана 15.11) без пісочниці;
перевірений тестами з підміною HTTP.

* **Режим.** `test_mode` — за хостом `MACROPAY_API_BASE`: бойовий лише `api.macropay.ai`, усе інше —
  пісочниця (помилкова адреса закриває купівлю, а не відкриває). Купівля в пісочниці зараховується
  й позначається як тестова (`marked_by = credit`), як у Creem.
* **Оформлення.** `POST /v1/checkouts/`: `products` [id товару з `providers.macropay` каталогу],
  `currency` `eur`, `success_url` (наш `checkout_ref` у шляху), `return_url` (кнопка «назад»),
  `metadata` {`account_id`, `package_id`, `checkout_ref`}. Персональних даних і `external_customer_id`
  не шлемо. Без id товару пакет через Macropay не купується (503).
* **Вебхук** — `POST /api/v1/billing/webhooks/macropay`, Standard Webhooks: HMAC-SHA256 над
  `{webhook-id}.{webhook-timestamp}.{тіло}` з розкодованим base64 секретом `whsec_…`; кілька підписів
  через пробіл (ротація), вікно 5 хвилин, порівняння сталим часом. Ідентифікатор події — `webhook-id`;
  другий захист — `order.id` (купівля) і `refund.id` (повернення).
* **Події.**

  | Macropay | Подія порту | Звідки |
  |---|---|---|
  | `order.paid` (стан `paid`) | `PurchaseCompleted` | `metadata`, `total_amount` (з податком), `currency`, `id` (транзакція й рядок) |
  | `refund.created` / `refund.updated` (стан `succeeded`) | `RefundApproved` | `id` (коригування), `order_id`, `amount` + `tax_amount`, `currency` |
  | `refund.*` `pending` / `failed` / `canceled` | `Unknown` | — |
  | `order.refunded`, `order.created`, `order.updated`, `checkout.updated` | `Unknown` | — |
  | будь-яка інша (зокрема подія спору, якщо з'явиться) | `Unknown` | — |

* **Повернення — часткові теж.** Адміністратор утримує юніти за замовленням
  (`billing_admin.py refund-hold --provider macropay --order <order.id>`), повертає гроші в кабінеті
  Macropay; перший `refund.*` зі станом `succeeded` завершує утримання, другий — повтор того самого
  `refund.id`. `order.refunded` несе накопичену суму без id повернення, тож лише записується як
  проігнорована: інакше юніти знялися б двічі. Повернення `failed` / `canceled` утримання не знімає —
  `refund-release`, як для Creem. `request_refund` (`POST /v1/refunds/`, `reason` `customer_request`)
  є, але його ще ніхто не викликає.
* **Спори.** Подій спору в каталозі Macropay немає: `Chargeback` адаптер не дає. Повернення з
  `reason: dispute_prevention` — звичайне повернення.

**Не перевірено — закриє жива перевірка в пісочниці:**

1. Конверт події — `{"type", "timestamp", "data"}` за специфікацією Standard Webhooks; `timestamp` —
   ISO 8601 з поясом. Тіла тестів зібрані з полів документації, не з її зразків.
2. Секрет `whsec_<base64>` декодується саме так (а не використовується як текст).
3. `order.paid` приходить зі `status: "paid"`; `currency` — малими літерами (`eur`).
4. Сума повернення в подіях: `amount` без податку, `tax_amount` окремо (порівнюємо `amount + tax_amount`
   з `total_amount`).
5. `amount` у `POST /v1/refunds/` — з податком чи без (журнал рахує з податком). Важливо, коли
   з'явиться виклик `request_refund`.
6. Який стан має повернення в `refund.created` (одразу `succeeded` чи `pending` → `refund.updated`).
7. Чи ручне повторне надсилання з кабінету зберігає `webhook-id`.
8. Чи приходить якась подія, коли спір усе-таки відкрито (шукати в `billing_provider_events` рядки
   `ignored` з невідомим `provider_event_type`).
9. Відповідь оформлення: поля `id` і `url`, код 201; що відповідає API на невідомий товар (зараз — 502).
10. `return_url` справді веде на кнопку «назад»; `success_url` повертається без дописаних параметрів.

**Що створити оператору в пісочниці Macropay:** Organization Access Token → `MACROPAY_API_KEY`;
кінцеву точку вебхука `https://<api host>/api/v1/billing/webhooks/macropay` (події `order.*`,
`refund.*`, `checkout.updated`), її секрет → `MACROPAY_WEBHOOK_SECRET`; чотири товари з разовою
фіксованою ціною в EUR (як `units-500-v1` … `units-10000-v1`) → їхні id у `providers.macropay` в
`config/billing_packages.yaml`. `MACROPAY_API_BASE` лишити типовим (пісочниця).

## Маршрути

| Маршрут | Хто | Що |
|---|---|---|
| `POST /portal/tasks/{id}/estimate` | студент | ціна перевірки: `price_units`, `available_units`, `enough`, `missing_units`, `charging_enabled`, `basis`, `paid_by` |
| `POST /nodes/{id}/documents/estimate` | автор (`prep`) | ціна обробки матеріалу: `price_units`, `available_units`, `enough`, `missing_units`, `charging_enabled`, `unpriced_reason`, `missing_features` |
| `POST /documents/{id}/reference/estimate` | автор (`prep`) | ціна пояснень ключа тесту (ті самі поля) |
| `GET /portal/billing/outlook` | студент | `reviews_left`, `warning`, `expiring_soon`, `paid_by`, `charging_enabled` |
| `GET /billing/outlook` | автор (`prep`) | `days_left`, `warning`, `expiring_soon`, `held_reviews`, `charging_enabled` |
| `GET /billing/spending?from=&to=` | автор (`prep`) | витрати в юнітах по курсах і завданнях |

**Оцінка перевірки студента.** `basis`: `typical` (типовий розмір роботи), `file` (надісланий файл),
`ceiling` (оцінку не вдалося отримати: `price_units` = N — «не більше N», спишеться факт). Перевірку
оплачує автор (`paid_by = "author"`): `price_units`, `available_units`, `missing_units` = `null`,
`enough = true` — подачу приймаємо завжди, чи почнеться перевірка, вирішує прогін. Відмова дверей —
422 `{"detail": {"code", "details"}}`, як і подача.

**Оцінка дії автора без ціни** (BIL-E3). Коли E порахувати не можна, `price_units` і `missing_units` —
`null`, `enough = true` (заздалегідь не відхиляємо: поріг за справжніми ознаками перевіряє старт
роботи), а `unpriced_reason` каже чому: `missing_features` — у тілі бракує ознаки, потрібної виду
матеріалу (`missing_features` — назви полів тіла: audio/video — `duration_sec`; presentation —
`slides`, `text_chars`; text/web — `text_chars`; code — `files`, `text_chars`; таблиця —
`cost_estimate.actions.REQUIRED_FACTORS`), або `estimate_failed` — збій самої оцінки. З ціною обидва
поля `null`.

**`charging_enabled`** у нагадуваннях — те саме значення й джерело, що в `GET /billing/spending` і
в оцінках автора (`charging.charging_enabled()`); решта полів від нього не залежить — що показувати,
поки списання вимкнене, вирішує екран. (Оцінка перевірки студента додає до перемикача ще тестовий
режим шляху завдання.)

**`held_reviews`** у `GET /billing/outlook`: `{count, oldest_at, missing_units}` — перевірки студентів
цього орендаря, які автор оплачує за правилом і які чекають його юнітів; `missing_units` = сума P
утриманих мінус доступні, не менше 0 (з кредитом — 0); `count = 0`, коли таких немає.

**`GET /billing/spending`** — межі включно, дати UTC; за замовчуванням поточний місяць. Відповідь:
`{period: {from, to}, charging_enabled, totals: {billed_units, covered_units}, courses: [{course_node_id,
title, billed_units, covered_units, reviews_units, processing_units, preparation_units, tasks:
[{authored_document_id, title, reviews, billed_units, covered_units}]}], unlinked: {billed_units,
covered_units}}`.

* `billed_units` — рухи `debit` + `difference_arisen` на рахунку автора, прив'язані до дій;
  `covered_units` — `benefit_covered` + `platform_covered` (покрито пільгою чи платформою, не
  виставлено). Купівлі, повернення, згорання, резерви не входять. Числа додатні.
* `reviews_units`, `processing_units`, `preparation_units` ділять `billed_units` курсу за видом дії.
  У `tasks` — перевірки й підготовка завдання; обробка — курсу, не завдання.
* Перевірка студента, яку автор оплачує за правилом, — під своїм завданням.
* Дія без курсу (рядок зник) — у `unlinked`, не губиться.
* Жодних доларів (замок-тест). `from` після `to` — 422 `invalid_period`.
* Поки списання вимкнене — нулі й `charging_enabled = false`.

## Звіт адміністратора про витрати

```bash
uv run python scripts/billing_admin.py costs [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--tenant ...] \
    [--by author|student|both|task] [--format table|csv]
```

Транзакція READ ONLY. Поруч із собівартістю USD — юніти з журналу (`costs.BilledUnits`), виставлені
(`debit` + `difference_arisen`) двома стовпцями за рахунком, на якому рух:
`виставлено_авторові` / `billed_author_units` (рахунок автора) і `виставлено_студентам` /
`billed_students_units` (рахунки студентів орендаря); окремо `покрито_юнітів` / `covered_units`
(пільга чи платформа — не виставлено нікому).

* рядок автора — рухи на рахунках орендаря;
* рядок студента — рухи перевірок для цього студента, хто б не платив (автор за правилом — у
  стовпці автора);
* `--by task` — автор → курс → домашнє завдання через `links.link_actions`; обробка — рядок курсу
  «(без завдання)», дія без курсу — «(без курсу)».

У шапці таблиці — рядок `costs.DATING_NOTE`: долари датовано викликом, юніти — рухом журналу, тож
на межі періоду вони можуть розійтися (CSV лишається чистою таблицею, без цього рядка).
