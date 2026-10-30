Доручення: Ментор / задача 10, ХТ-2 (зондування інтеграції) · Для: сесія «Ментор»

# PROBE-10 — як вбудувати посилання на курс у конвеєр рецензії

Зондування без змін коду. База — `main` @ `55e81a7` (2026-10-30). Жодних міграцій, викликів
моделей чи доступу до проду не було. Посилання на код подано як файл і символ, номер рядка — лише
орієнтир.

## Коротко

1. **Контрольна точка не зберігає виходів стадій. Так задумано.** Вердикти й пояснення лежать в
   окремих таблицях, а точка лише позначає стадію як `done`. Кандидатів за тим самим зразком треба
   класти в окрему таблицю: це **одна мінімальна міграція**. Вибір моделі можна зберегти **без
   міграції** — він ляже в `submission_explanations.body` разом з її відповіддю.
2. **Крок без моделі зараз не оголосити.** `PathStage` вимагає промпт, непорожню драбину й
   `money_usd > 0`. Потрібен вид стадії «код» у `homework/path_config.py`.
3. **Шлях `project` не має стадій оцінки й пояснення, а тип — збирача.** Вставити крок «після
   оцінки, перед поясненням» у проєкт зараз нема куди. Це розбіжність із рішенням 1, її має
   розв'язати сесія «Ментор».
4. **Для адаптера пошуку (його пишуть інші доручення).** `search_by_concepts` **не фільтрує
   матеріали за роллю**: методичні матеріали, зокрема еталонні розв'язки, можуть дійти до студента.
   Крім того, він сортує за сиблінговим `CourseNode.order`, а це не порядок навчання. Тому ознака
   «далі в курсі» потребує обходу дерева в прямому порядку.
5. **`Reference.url` обов'язкове, а URL матеріалу в порталі бекенд не знає.** Це відкрите питання
   до UI.
6. **`record_review_metrics` не викликає ніхто.** Найкраще місце для виклику —
   `TextResultBuilder.after_delivery`.
7. **SQL-обрізання тексту в коді репозиторію немає.** Нижче — гіпотеза про прод і як її
   перевірити.
8. **course-supporter-ui прочитати не вдалося.** Репозиторій існує, але підключити його до цієї
   сесії не дозволив шар дозволів сесії.
9. **Конфлікт імен.** «reference» у коді вже означає ключ тесту (`TaskReference`,
   `homework/reference_service.py`, `reference_key.py`). Для нового кроку раджу інше слово:
   `course_link…`.

---

## 1. Контрольна точка: де стан стадій і звідки пояснення бере вердикти

**Факти.**

- `homework/path_checkpoint.py::PathCheckpoint` — це JSON у `Job.stage_progress`. Поля:
  `task_type`, `submission_state`, `stages` (`pending`/`done`), `frozen_stage`, `frozen_reason`,
  `retries`, `started_at`, `updated_at`; `extra="forbid"`. Нове поле має мати значення за
  замовчуванням (так каже докстрінг модуля).
- Запис і читання:
  - `save_checkpoint` пише й **комітить**.
  - `load_checkpoint` читає останню роботу ревізії, яка точку записала
    (`JobRepository.get_latest_for_subject(..., require_stage_progress=True)`).
  - `path_runner._run_path` переписує відновлену точку в нову роботу.
- Порядок усередині стадії:
  - Виконавець пише **свій** вихід у сесію тіла. Чіпати точку йому заборонено (докстрінги
    `path_stages.StageContext`, `StageOutcome`).
  - Тіло після стадії робить `with_stage_done` і `save_checkpoint`. Це один коміт, тож вихід
    стадії і позначка `done` фіксуються разом (`_run_path`, цикл по `remaining`).
- Вердикти:
  - Стадія оцінки пише рядки `submission_criterion_verdicts` (`storage/orm.py::SubmissionCriterionVerdict`,
    `storage/submission_verdict_repository.py::SubmissionVerdictRepository.replace_for_submission`).
  - Стадія пояснення читає їх через `list_for_submission`, а перелік критеріїв — за адресою з
    рядків (`homework/review_explanation.py::explain_verdicts`, `_judged_list`).
- Пояснення зберігається в `storage/orm.py::SubmissionExplanation.body` (JSONB): один рядок на
  подачу, повторний запуск його замінює (`store_explanation`). Докстрінг таблиці прямо каже: *«a
  path's checkpoint keeps no stage output»*.

Отже, «зберігати так само, як вердикти» означає окрему таблицю, яку пише виконавець, а точка
позначає стадію як `done`.

**Варіанти для кандидатів.**

| | що | міграція | мінуси |
|---|---|---|---|
| A | нове поле `PathCheckpoint.course_link_candidates` з `default={}` | ні | ламає два інваріанти (точка без виходів стадій; виконавець не чіпає точку) → потрібні вантаж у `StageOutcome` і правка тіла; вихід живе на `Job`; після відкату коду точка «нечитана» → шлях з нуля, оплата вдруге |
| B | дописати в `submission_explanations.body` | ні | рядок належить іншій стадії, `store_explanation` замінює його цілком; `language NOT NULL` + CHECK; збирач валідує `body` як `ExplanationAnswer` (`extra="forbid"`) |
| C | нова таблиця `submission_course_link_candidates` за зразком `submission_explanations` | **так, одна** | — |

**Рекомендація — C.**

- Колонки: `id` (uuid7), `tenant_id` (FK, CASCADE), `submission_id` (FK, CASCADE, унікальний
  індекс), `body` JSONB, `created_at`, `updated_at`.
- `body` — відповідь порту як вона є: для кожного критерію до трьох кандидатів (`segment_id`,
  `authored_document_id`, назва матеріалу, `source_type`, сирий якір, текст позиції,
  `later_in_course`, `confidence`, ужиті поняття).
- Повторний запуск кроку замінює рядок.
- Міграцію писати руками від `esc_billing_grade_cost`. Зразок — `migrations/versions/submission_verdict_tables.py`.
- Методи сховища: `store_candidates` / `get_candidates` у `SubmissionVerdictRepository` або окреме
  сховище поруч.

**Вибір моделі — без міграції.** Нове поле `links: tuple[str, ...]` у `CriterionRemark`
(`homework/verdict_explanation.py`) лягає в `SubmissionExplanation.body` разом з відповіддю (див.
§6). Зберігати варто вже очищений список: «вигадки» викинуто, слід — у реєстрі (§5).

## 2. Конфігурація шляху: куди вставити крок і чи це щось змінює для проду

**Факти.**

- `config/submission_paths.yaml`:
  - `short_task` і `task`: `first: [safety, attempt_classifier, criteria_evaluation, review_explanation]`,
    обидва `repeat_*: [safety, criteria_evaluation, review_explanation]`.
  - `project`: `[safety, attempt_classifier]` / `[safety]` — **без оцінки й пояснення**.
  - `served_by: todays_mentor` у трьох типів, `new_path` — лише в `test`.
- `homework/path_config.py::PathStage` — усі поля обов'язкові: `prompt_ref`, `requires`,
  `input_budget_ratio`, `record_output`, `ladder` (`min_length=1`), `deterministic`, `ceilings`
  (`money_usd: PositiveFloat`). `validate_path_config` перевіряє, що промпт читається, і проганяє
  кожен щабель через реєстр моделей.
- `boot.py` в обох процесах викликає `validate_path_config` і
  `validate_stage_executors(path_config.stages)`. Друга перевірка йде по **всіх оголошених**
  стадіях, а не лише по тих, що стоять у шляхах: стадія без виконавця зупиняє підйом.
- `path_runner._run_path` після кожної стадії викликає `port.account_stage_cost(_stage_cost(...))`
  (сума рядків реєстру з `action == ім'я стадії`). Для стадії без викликів моделі це 0, і так і має
  бути.
- `get_result_builder` (`homework/result_builders.py`) не має `PROJECT`.

**Чому для проду нічого не змінюється.**

- `path_runner.run_new_path_if_switched` читає подачу й точку, далі `path_selection.choose_path`
  повертає `None`, коли `served_by is not ServedBy.NEW_PATH`. Для відновленої точки так само працює
  `_choice_from`.
- Тіло відповідає `False` до дверей і до будь-якої стадії, тож новий крок у переліках `short_task`,
  `task` чи `project` подач не зачіпає.
- **Застереження:** перевірка на старті читає весь файл незалежно від перемикачів. Битий опис нової
  стадії, відсутній виконавець чи промпт `v2` зупинять **підйом проду**. Це сторожує
  `tests/unit/test_homework/test_submission_paths_file.py::test_file_passes_the_startup_checks`.
- Тест `test_text_review_e2e_db.py::TestTheShippedFileLeavesTextTasksWithTodaysMentor` (замок 8)
  лишається зеленим.

**Як оголосити крок без моделі.**

- **A. Вид стадії «код»** у `path_config.py`:
  - `PathStage` стає об'єднанням `ModelStage | CodeStage`. `CodeStage` суворий:
    `{code: true, deterministic: bool}`, без драбини, промпту й стель.
  - Перевірки промпту й щаблів — лише для `ModelStage`.
  - `path_ceiling_estimate` рахує для коду 0.
  - `path_stages._execution` будується лише для моделі.
  - Правило «ім'я не повторює стадію драбин» лишається для обох видів.
- **B. «Фіктивна» модельна стадія** (драбина-заглушка, промпт-заглушка, `money_usd: 0.01`). Підйом
  пройде, але конфігурація каже неправду, завищує оцінку стелі для порту коштів і перевіряє промпт,
  якого ніхто не надсилає. Відкинути.
- **C. Пошук на початку виконавця пояснення.** Конфігурація не змінюється, але це суперечить
  рішенню 1: окремий крок зі своїм станом у точці. Лише як запасний варіант.
- Якщо адаптер пошуку виявиться платним (ембедінги, переранжування моделлю), крок за природою стає
  модельною стадією. Тоді A не потрібен, а «драбина» стає справжньою.

**Рекомендація.**

- Варіант A, ім'я стадії `course_link_lookup`.
- Вставити між `criteria_evaluation` і `review_explanation` у всі три шляхи `short_task` і `task`.
- Виконавець — `path_stages.run_course_link_lookup_stage`, робота — у новому модулі
  `homework/course_links.py`.
- Для `project` крок буде доданий разом зі стадіями оцінки й пояснення проєкту. Це поза задачею 10:
  потрібне рішення сесії «Ментор» щодо рішення 1.

## 3. Порт: зразки в репозиторії й де покласти новий

**Зразки.** Усі — структурні `Protocol` без базового класу, із розділом «Replacing» у докстрінгу.

| порт | де | як підставляється в тестах |
|---|---|---|
| `FundsPort` + `AlwaysEnoughFundsPort` | `funds_port.py` | параметр `funds_port`; e2e патчить `path_runner.AlwaysEnoughFundsPort` (`test_text_review_e2e_db.py::_run`) |
| `CriteriaSource` | `homework/criteria_evaluation.py` | параметр `criteria_source=None` → прод `build_criteria_list_service(...)`; e2e патчить `criteria_evaluation.build_criteria_list_service`, стадійний тест передає параметр (`test_criteria_evaluation_stage_db.py`) |
| `ResultBuilder` | `homework/result_builders.py` | патч `get_result_builder` у `path_runner` |
| `ReviewMetricsCalculator` | `review_metrics.py` | передається параметром |
| `EntryClassifier`, `TextExtractor` | `normalizer/ports.py` (окремий модуль портів) | ін'єкція |
| виконавець стадії | `path_stages.register_stage_executor` | заміна під тим самим ім'ям |

**Де класти.** Порт споживають двоє: крок пошуку (§2) і повторний пошук у збирачі (§5). Тому
краще окремий модуль `homework/course_links.py`, у ньому:

```python
class CourseLinkFinder(Protocol):
    async def find(
        self, scope: SubmissionScope, failed: Sequence[FailedCriterion]
    ) -> Mapping[str, tuple[LinkCandidate, ...]]: ...   # criterion id → ≤ 3

# SubmissionScope: tenant_id, course_root_id (= submission.course_node_id),
#                  task_node_id (= submission.node_id), authored_document_id
# FailedCriterion: id, text, concepts            (Criterion.concepts уже є, criteria_form.py)
# LinkCandidate:   segment_id, authored_document_id, material_title, source_type,
#                  anchor (SegmentAnchor), excerpt, later_in_course, confidence
```

- Продовий адаптер повертає фабрика `build_course_link_finder(session)`. Тести патчать саме її, за
  зразком `build_criteria_list_service`.
- Адаптер над `storage/document_segment_repository.py::DocumentSegmentRepository.search_by_concepts`
  пишуть інші доручення.

**Два факти для адаптера** (вони впливають на інтеграцію):

- `search_by_concepts` не фільтрує за `AuthoredDocument.material_role`. Кандидат має бути лише з
  матеріалу, видимого студентові (`api/routes/_portal_shared.py::role_visible_to_student`). Інакше
  студент отримає посилання на методичку чи еталонний розв'язок.
- Сортування `CourseNode.order → created_at → AuthoredDocument.order → DocumentSegment.order` — це
  не порядок курсу: `CourseNode.order` — ключ серед сиблінгів (коментар колонки в `orm.py`). Для
  `later_in_course` потрібне порівняння позицій у прямому обході дерева від кореня.

## 4. Форма посилання

**Факти** (`models/review_structure.py`):

- `Reference{title, url}` — обидва поля обов'язкові, `min_length=1`, `extra="forbid"`.
- `Remark{what, why, todo, read: list[Reference]=[], position: Position | None}`.
- `Position{kind: PositionKind, value: str}`, де `PositionKind = "video" | "slide" | "paragraph" | "file"`.
- `Remark.position` не заповнює жоден збирач: `Position(` поза модулем у `src/` не трапляється.
- Збирач тексту (`homework/review_assembler.py::_remark_block`):
  - позицію друкує фразою `position.{kind}`;
  - `read` друкує як `- **{remark.read}:** [title](url), …`.
- Якір фрагмента — `storage/document_segment_repository.py::SegmentAnchor.kind`:
  `"time" | "slide" | "paragraph" | "file"`. Відео **й аудіо** → `time` (`_resolve_anchor`). Для
  тексту чи вебу на межі KD-C буває `("paragraph", None, None)`.

**Які поля додати до `Reference`.** Усі необов'язкові, версія схеми лишається «1»:

| поле | тип | нотатка |
|---|---|---|
| `position` | `Position \| None` | наявна модель (вид + значення), а не два окремі поля |
| `excerpt` | `str \| None`, `min_length=1` | текст позиції; обрізати **в Python за символами** (див. §11) |
| `segment_id` | `str \| None` | UUID фрагмента в знімку |
| `later_in_course` | `bool = False` | друкується фразою розмовника (§8) |

**`url`.** У бекенді немає стабільного URL матеріалу в порталі. Є лише API
`GET /portal/materials/{authored_document_id}` (`api/routes/portal_materials.py::get_portal_material`)
і `settings.portal_base_url` (його вживає `portal_recovery`). Структура йде **цілою** у вебхук до
платформи школи (`homework/webhook.py` → `curated_structure`), тому URL має бути абсолютним.

- Варіант (а): одна функція `material_url(tenant_id, authored_document_id, position)` →
  `{portal_base_url}/{tenant_id}/…/{authored_document_id}#t=…`. Маршрут треба підтвердити в UI.
- Варіант (б): зробити `url` необов'язковим. Це вже зміна наявного поля, а не просте розширення.
- **Рекомендую (а).** Відкрите питання до сесії «Ментор» або UI.

**`time` проти `video`.**

- (1) У коді відобразити `time → video`. Аудіо тоді надрукується як «відео, 03:41» — неправда.
- (2) **Розширити** `PositionKind` значенням `audio` і додати фразу `position.audio: аудіо, {time}`.
  Це додавання, тож старі рецензії читаються. Вид визначається з `source_type`: video→video,
  audio→audio, presentation→slide, text/web→paragraph, code→file.
- (3) Перейменувати `video` на `time`. Це не розширення і ламає ключ фрази в 60 файлах.
- **Рекомендую (2).**
  - Одна чиста функція `position_of(anchor, source_type) -> Position | None`.
  - Секунди форматуються як `mm:ss` або `h:mm:ss`.
  - Абзац без номера дає `None`: посилання без позиції.
  - Нова фраза їде тим самим запуском перекладу, що й «далі в курсі» (§8).

**Як портал перевіряє версію.** `api/routes/_portal_shared.py::curated_structure`:

- Якщо `review_schema_version(...) != "1"`, повертає `None`.
- Інакше робить `ReviewStructureV1.model_validate`. Невалідна рецензія дає лог
  `review_structure_invalid` і `None`.
- Тією самою функцією користується вебхук, а також `curated_verdict` і `version_1_outcome`.

Стара рецензія версії 1 без нових полів **проходить**, бо поля необов'язкові. У зворотний бік ризик
є: рецензію з новими полями старий процес (`extra="forbid"`) прочитає як `None`. Це стосується лише
відкату коду. На проді текстових рецензій версії 1 немає, бо типи не перемкнуто, тож зараз ризик
нульовий.

## 5. Збирач результату: повторне збирання, перевірка перед видачею, реєстр

**Факти.**

- `path_runner._run_path`: після стадій ставиться статус `reviewing`, далі
  `builder.build(...)`, **якщо `entered_as != completed`**.
  - Повторний вхід зі стану `reviewing` (працівник помер між збиранням і завершенням)
    **перескладає** рецензію.
  - Повторний вхід із `completed` лише доставляє збережене.
- `homework/text_result.py::TextResultBuilder.build` читає рядки вердиктів і пояснення, потім
  викликає чисту `build_text_review`, яка повертає `Remark(what, why, todo)` без `read`.
- `BuildContext.session` — **лише для читання**: «a builder READS through it and writes nothing».
  Писати через `session_factory` можна в `after_delivery`.

**Куди вписати перевірку.** У `TextResultBuilder.build`, перед `build_text_review`:

1. Прочитати кандидатів (§1, C) і вибір із `explanation.body`.
2. Для кожного вибраного: фрагмент живий (`DocumentSegment.id`, `deleted_at IS NULL`, матеріал
   живий і видимий студентові) → **знімок** зі збереженого кандидата.
3. Фрагмент зник → `CourseLinkFinder.find` за тими самими поняттями критерію. Поняття беруться з
   переліку за адресою рядків вердиктів: `review_explanation._judged_list` варто зробити спільним.
   Мікрорішення: якою заміну обирати. Раджу брати кандидата з того самого матеріалу, а якщо такого
   немає — найвпевненішого.
4. Нічого не знайдено → посилання знімається, у реєстр іде «застарілість».
5. Готові `Reference` передаються в `build_text_review(..., links=…)`, функція лишається чистою.

Збирач нічого не пише в таблиці кандидатів і пояснень. Тому «повторне збирання не змінює збережених
посилань» виконується за побудовою.

**Реєстр.** Це журнал викликів `external_service_calls` через `service_logging._persist`.

- Рядки без виклику там уже є: `record_review_metrics` (`action="review_metrics"`) і
  `record_funds_decision`. У них `success`, `provider`, `model_id` — NULL.
- Новий записувач `record_course_link_trace(session_factory, *, kind, criterion_id, ids)`:
  - `action` — `course_link_fabricated` або `course_link_stale`;
  - подробиці (лише ідентифікатори, без тексту студента) — у `error_message`;
  - плюс рядок structlog.
- Міграція **не потрібна**: `action` — вільний `String(100)` без CHECK, `outcome` може бути NULL.
- `_persist` пише лише в контексті роботи (`_under_current_job`). І стадія, і збирач працюють
  усередині ARQ-задачі.
- З `_stage_cost` це не перетинається, бо ця функція рахує суму за `action == ім'я стадії`.
- Повторне збирання дасть другий рядок «застарілості». Це прийнятно, або можна прибрати дублікати
  за `job_id`.

## 6. Промпт пояснення: версія 1 і що змінити для версії 2

**Факти.**

- Файл `prompts/review_explanation/v1.md` (розділи `## System` / `## User`, Jinja). Слоти:
  `<assignment_title>`, `<assignment_description>`, `<assignment_text>`, `<result>`, `<verdicts>`,
  `<student_submission>`.
- **Замок слоту** (задачі 11 і 09б), `llm/prompt_loader_md.py`:
  - `slot_names`: слот — це тег, що стоїть сам у рядку.
  - `lock_slots` і `_finalizer`: кожен вивід `{{ }}` у тегах слотів **цього** шаблону міняє `<` на
    `&lt;`.
  - JSON іде через фільтр `tojson_unicode` (`llm/prompt_json.py::prompt_json`), який екранує `<`/`>`
    як `<`/`>`.
  - Замки: `tests/unit/test_llm/test_prompt_slot_lock.py` (випадок
    `test_the_review_explanation_keeps_the_submission_in_its_slot`) і
    `tests/unit/test_prompts/test_prompt_json_locks.py::test_review_explanation_prompt`.
- Схема виходу — `verdict_explanation.ExplanationAnswer{passed, why, remarks[CriterionRemark{id, what, why, todo}], mentor_voice}`.
  На дроті — `agents/review_explainer.py::RESPONSE_SCHEMA = wire_schema(ExplanationAnswer)`:
  суворо на qwen3.7-max, JSON-режим на deepseek-flash.
- Звірка з фактами: `verdict_explanation.read_explanation` → `_disagreements` (`passed`, рівно одна
  примітка на кожен невиконаний критерій, порожніх текстів немає). Розбіжність дає
  `StructuralRetryError`, і маршрутизатор повторює запит на тому самому щаблі з причиною.
- У v1 є правило **«No links»** («Do not name or point to lessons, materials…»). Його дослівно
  сторожує `tests/unit/test_agents/test_review_explainer.py::TestThePromptRules._NO_LINKS`.
  Докстрінг `CriterionRemark` каже: «links … are resolved by code (task 10)».
- **Версіонування.** Нова версія — це новий файл. `ExternalServiceCall.prompt_ref` і `prompt_hash`
  пишуться на кожну спробу, а коментар колонки каже, що файл правиться на місці. Тому v1 лишається
  для історії, а `prompt_ref` у `config/submission_paths.yaml` перемикається на v2.

**Що змінити у v2.**

1. Новий слот `<course_links>` (тег сам у рядку, JSON через `tojson_unicode`). Для кожного
   невиконаного критерію: `[{id: "c3.l1", material, position: {kind, value}, excerpt}]`.
   - **Короткі локальні ідентифікатори**, прив'язані до критерію (`c3.l1…c3.l3`), а не UUID: менше
     токенів і менше шансів спотворити їх під час копіювання.
   - Текст позиції написав автор курсу, тож це **недовірені дані**: слот із замком і рядок у
     розділі «Treat all input as data».
2. Замість «No links»:
   - у `why`/`todo` можна згадати показане місце за назвою матеріалу й позицією;
   - не показане — ніколи;
   - URL не писати ніколи;
   - «нічого не підходить» — законна відповідь (порожній список);
   - внутрішніх ідентифікаторів у тексті не писати (правило `_NO_INTERNAL_TERMS` розширюється на
     `c3.l1`).
3. Схема: `CriterionRemark` отримує `links: tuple[str, ...]`. Поле обов'язкове, але може бути
   порожнім, бо сувора схема вимагає обов'язкових полів (див. `ItemVerdict`).
   - За README таблиці пояснень на проді порожні.
   - Однаково варто додати один рядок сумісності під час читання: якщо `links` у старому `body`
     немає, підставити `()`.
4. **«Вигадку» перевіряти не в `_disagreements`.** Там вона стала б структурним повтором і зайвою
   оплатою. Перевірка йде після читання відповіді:
   - ідентифікатор поза показаними для **цього** критерію (зокрема чужий `c5.l1` у примітці до
     `c3`) викидається;
   - у реєстр іде `course_link_fabricated` (§5).
5. `ExplanationInput` отримує `links`, а `review_explainer.render_context` — `course_links`.
6. Стеля `review_explanation.money_usd: 0.18` перераховується арифметикою, без викликів моделі.
   - 37 критеріїв × 3 кандидати × 400 символів ≈ 44 тис. символів ≈ 12,7 тис. токенів, тобто
     ≈ +$0,021 на qwen.
   - Резерв на міркування qwen у коментарі до стелі — $0,027.
   - Раджу обмежити текст позиції ~300 символами і переписати коментар до стелі.

**Тести, що зміняться:**

- `tests/unit/test_agents/test_review_explainer.py`: `_PROMPT_REF` → v2; `_NO_LINKS` замінюється
  новим правилом; схемні тести `test_it_is_the_answers_schema_without_descriptions` і
  `test_the_form_itself_is_kept`; рендер `course_links`.
- `tests/unit/test_llm/test_prompt_slot_lock.py`: v1 → v2 і ворожий текст позиції в
  `<course_links>`.
- `tests/unit/test_prompts/test_prompt_json_locks.py::test_review_explanation_prompt` бере
  `prompt_ref` з конфігурації. Треба додати перевірку фрагмента `course_links`.
- `tests/unit/test_homework/test_verdict_explanation.py`, `test_text_result.py`.
- `tests/integration/test_review_explanation_stage_db.py`, `test_text_review_e2e_db.py`: записані
  `_EXPLANATION` / `_remark` отримують `links`.
- `tests/unit/test_homework/test_submission_paths_file.py`
  (`test_a_text_task_is_judged_then_explained_on_every_path` — переліки стадій).

## 7. `authenticity`

**Факти.**

- `review_metrics.py`:
  - `Claim{supported_by_reference, covered_by_verdict}`, `ReviewMetrics`;
  - `ReviewMetricsCalculator` (`Protocol`) і `ShareMetricsCalculator`;
  - `REVIEW_METRICS_ACTION = "review_metrics"`.
- Записувач — `service_logging.record_review_metrics(session_factory: async_sessionmaker[AsyncSession], metrics: ReviewMetrics) -> None`.
  Він кличе `_persist(action="review_metrics", provider=None, model_id=None, success=None, authenticity=…, completeness=…)`.
  Колонки — `ExternalServiceCall.authenticity` і `completeness` (Float).
- **Викликачів у `src/` немає.** Є лише тести: `tests/unit/test_review_metrics.py`,
  `tests/integration/test_review_metrics_db.py`, `tests/unit/test_service_logging.py`.
- Чому (висновок із коду й історії):
  - Модуль додано як шов у задачі 01 (`0b2a95a`, 2026-09-14).
  - Докстрінг відкладає перетворення рецензії на твердження до збирача.
  - До задачі 10 жоден збирач не давав посилань, тож мірка була б завжди 0 або `None`, і її не
    підключили.

**Де кликати.** У `TextResultBuilder.after_delivery` (зараз він порожній). Це місце для того, що
«may fail without taking back what the student already has», і там пише окрема сесія.

- Метод читає збережену `submission.review_result` як `ReviewStructureV1`.
- Твердження — одна примітка `new_remarks`. `supported_by_reference = bool(remark.read)`,
  `covered_by_verdict = True`: кожна примітка прив'язана до невиконаного критерію через
  `_disagreements`.
- Чому не в `build`: воно може виконатися двічі й нічого не пише.
- Застереження:
  - `_after_delivery` виконується і при повторному вході з `completed`, тож рідко можливий дубль
    рядка.
  - `completeness` для текстового шляху тривіально дорівнює 1,0. Записувач пише обидві мірки, тож
    треба вирішити: писати 1,0 чи передавати `None`.

## 8. Розмовник

**Факти.**

- Файли `config/phrasebook/<iso3>.yaml`, 60 мов, плоскі `ключ: текст` (з `reviewed: true` —
  `Phrase`).
- Джерело — `ukr.yaml` (`SOURCE_LANGUAGE`), запасна мова — `eng`.
- Читання — `phrasebook/loader.py::phrases_for`.
- `phrasebook/validation.py::validate_phrasebook` викликається на **старті** (`boot.py`): набір
  мов збігається з дозволеним, кожна мова має кожен ключ, плейсхолдери збігаються. Ключ лише в
  `ukr.yaml` **зупинить підйом**, тож фраза має з'явитися в усіх 60 файлах одним комітом.

**Як додати.**

1. Додати ключ у `ukr.yaml`, наприклад `link.later_in_course: Докладніше — далі в курсі`, а також
   `position.audio: аудіо, {time}`, якщо приймається §4 (2).
2. Оцінка без викликів:

   ```bash
   uv run python -m scripts.translate_phrasebook --dry-run --keys link.later_in_course,position.audio
   ```

3. Справжній запуск — той самий рядок без `--dry-run`.

**Як працює `scripts/translate_phrasebook.py`.**

- Один виклик на мову, усі ключі в одному запиті, англійська йде першою. Це 59 викликів (60 мов
  мінус джерело).
- Стадія `dictionary_translation`, `config/ladders_dictionary.yaml`: один щабель
  `gemini/gemini-3.8-flash`, `max_output_tokens: 8192`. Докстрінг скрипта досі згадує
  `gemini-2.5-pro` — він застарів.
- Кожна мова пишеться на диск одразу після перевірки плейсхолдерів, повторів усередині запуску
  немає, перевірених людиною фраз скрипт не чіпає.
- Відмовляється стартувати, якщо оцінка перевищує $6 (`COST_CEILING_USD`).
- **У реєстр не пише** (маршрутизатор без фабрики сесій), тож вартість читається на сторінці Usage
  провайдера «до» і «після».

**Вартість однієї фрази (або двох — кількість викликів та сама).** Ціни
`config/external_services.yaml`: $0,0015 / 1 тис. на вході, $0,0075 / 1 тис. на виході; до
2026-12-31 діє вступна знижка вдвічі.

- Оцінка, яку надрукує скрипт: ≈ 430 токенів на вході ($0,0006) + стеля виходу 8192 ($0,061) ≈
  $0,062 на виклик, × 59 ≈ **$3,7**, тобто нижче стелі $6.
- Реалістично: відповідь — десятки токенів плюс міркування (модель міркує, і це не обмежити) ≈
  $0,002–0,01 на виклик, **≈ $0,1–0,6 за запуск**.
- Запускає оператор зі своїми ключами.

## 9. Наскрізний тест

**Що вже є.** `tests/integration/test_text_review_e2e_db.py` (09б, K6):

- Реальні: ARQ-задача, шов, тіло, чотири стадії з описами з файлу, сховище, збирач, вебхук.
- Дублери:
  - `_RouterDouble` — записана відповідь на ім'я стадії, валідатор стадії, рядок реєстру з ціною;
  - `_ListSource` — через патч `criteria_evaluation.build_criteria_list_service`;
  - `_PortDouble` — патч `path_runner.AlwaysEnoughFundsPort`;
  - транспорт вебхука.
- `_switched()` перемикає `task` лише в тесті.
- Фікстура `world`: `Tenant`, корінь `CourseNode` (`ukr`), `AuthoredDocument(task_type="task")`,
  готовий перелік (`TaskCriteriaListRepository.claim` / `mark_ready` з `_criterion(cid, weight, points)`).
- Поруч: `test_path_e2e_stages_db.py`, `test_new_path_pipeline.py`, `test_criteria_road_e2e_db.py`,
  `test_review_explanation_stage_db.py`, `test_criteria_evaluation_stage_db.py`.
- CI ганяє тести з базою: `.github/workflows/ci.yaml`, `pytest --run-db --run-redis`.

**Рекомендація.**

- Новий файл `tests/integration/test_course_links_e2e_db.py`, бо в наявному вже 1047 рядків.
- Клас `TestAFailedCriterionGetsALinkToAPlaceThatExists`, тест
  `test_the_review_links_the_segment_the_explanation_chose`.
- Дублери `_RouterDouble`, `_PortDouble`, `_switched` винести в `tests/_helpers/` (наприклад,
  `text_path.py`) або повторити.

**Звідки беруться дані.**

- **Критерій:** `_criterion(...)` з `concepts=("рекурсія",)` (`Criterion.concepts`).
- **Матеріали курсу:** у тому самому корені — `AuthoredDocument(source_type="video",
  material_role="educational")`, `DocumentSummary` і `DocumentSegment(main_concepts=["рекурсія"],
  start_time_sec=221, content=…)`. Хелпери засіву є в
  `tests/integration/test_document_segment_repository.py` («search_by_concepts seed helpers»).
  Плюс методичний матеріал з тим самим поняттям, щоб перевірити, що він **не** потрапляє в
  кандидати.
- **Адаптер пошуку:** справжній, бо він безплатний. Дублюється лише те, що платне, — така
  філософія файла.
- **Відповідь моделі:** записаний `_EXPLANATION` з `links: ["c2.l1"]`.

**Що перевіряти:**

- `review_result.new_remarks[i].read[0]` дорівнює `Reference(title=…, url=…,
  position=Position(kind="video", value="03:41"), excerpt=…, segment_id=str(seg.id),
  later_in_course=False)`;
- фраза в markdown;
- вебхук несе те саме.

**Супутні проходи.**

- K5: `links: ["c2.l9"]` → посилання немає, є рядок `course_link_fabricated`.
- K6: фрагмент м'яко видалено перед повторним збиранням → заміна або рядок `course_link_stale`;
  рядок `review_metrics` з `authenticity`.

## 10. Типи порталу (course-supporter-ui)

**Типів не прочитано.** Репозиторій `KonstZiv/course-supporter-ui` існує, але підключити його до
цієї сесії не дозволив шар дозволів сесії. Обходити це я не став. Там треба перевірити:

- типи структури рецензії: `Reference`, `Remark`, `Position`, вид позиції;
- чи розбирає UI суворо (невідомі ключі);
- як малюється `read`;
- маршрут матеріалу для `url` (§4);
- обсяг правки невідомий.

## 11. Обрізання тексту засобами SQL

**Перелік місць у коді — порожній.** Пошук по `src/`, `scripts/`, `tools/`, `migrations/`,
`tests/` — `func.left/right/substr/substring/overlay/split_part/convert/encode`, `.substr(`,
`type_coerce`, сирий `left(`, `substr(`, `substring(`, `::varchar(n)`, `convert_to(` — дав такі
збіги:

- `migrations/versions/a1b2c3d4e5f6_phase1_rename_and_kd3.py::upgrade`: `substring(title, 1, 80)`
  лише в тексті повідомлення `RuntimeError` (запит для ручної перевірки), не виконується.
- Звуження колонок (`material_macro_sections.title` → 128; `downgrade` у
  `external_strings_text_.py`) — це помилка на задовгому значенні, а не обрізання. `varchar(n)`
  рахує символи.
- Обрізання за **байтами** в Python, обидва з `decode("utf-8", errors="ignore")`, тож битий UTF-8
  у базу не потрапляє:
  - `api/tasks.py::_bound_safety_text` (1,5 МіБ, текст для перевірки безпеки);
  - `ingestion/code_skeleton.py::cap_skeleton`.
- `DocumentSegment.content` у коді ніде не обрізається.

**Гіпотеза для проду** (перевірити до К4, бо К4–К5 братимуть текст позиції з `content`).

- `left()` / `substr()` у PostgreSQL рахують **символи**, коли кодування бази `UTF8`.
- Якщо база має `server_encoding = SQL_ASCII`, ці функції рахують **байти**:
  - ріжуть кириличну літеру навпіл;
  - конвертації немає, тож клієнт (asyncpg / psycopg / psql з `client_encoding=UTF8`) падає з
    помилкою розкодування;
  - а сам `content` (цілий) читається нормально.
- Це збігається з описом симптому.
- Перевірка (лише читання): `SHOW server_encoding; SHOW client_encoding;` і
  `SELECT datname, pg_encoding_to_char(encoding), datcollate FROM pg_database;`.
- Прод-образ — `pgvector/pgvector:pg17` (`docker-compose.prod.yaml`). Явного кодування в
  `scripts/init-db.sh` і компоузі немає, тож образ за замовчуванням дає UTF8. Але том
  `pgdata-cs` міг бути створений інакше. Обрізання, що падало, ймовірно, було разовим запитом
  поза кодом.
- **Правило для нових комітів:** текст позиції обрізати в Python за символами (`content[:N]`), не
  в SQL.

---

## Підсумок: розклад решти задачі на коміти

| | К4 — форма посилання, збирач, розмовник | К5 — крок пошуку, промпт v2, «вигадка» | К6 — перевірка перед видачею, «застарілість», `authenticity` |
|---|---|---|---|
| **що змінюється** | `Reference` + `position`/`excerpt`/`segment_id`/`later_in_course`; `PositionKind` + `audio`; чисті `position_of`, `material_url`; `review_assembler._remark_block` друкує позицію, текст, ознаку; таблиця кандидатів + сховище; `TextResultBuilder` читає кандидатів і вибір і робить знімок (поки виробника немає → вихід ідентичний сьогоднішньому); фрази `link.later_in_course` (+ `position.audio`) у 60 файлах | вид стадії «код» у `path_config.py`; стадія `course_link_lookup` у шести шляхах `short_task`/`task`; `homework/course_links.py` (порт, фабрика, робота стадії, запис кандидатів); виконавець у `path_stages.py`; `prompts/review_explanation/v2.md`, `prompt_ref` → v2; `CriterionRemark.links`; `ExplanationInput`/`render_context`; фільтр «вигадки» + `record_course_link_trace`; перерахунок `money_usd` | у `TextResultBuilder.build`: перевірка існування, повторний пошук через порт, зняття + `course_link_stale`; `after_delivery` → твердження → `record_review_metrics`; наскрізний тест `test_course_links_e2e_db.py` (основний прохід + «вигадка» + «застарілість») |
| **міграція** | **так, одна**: таблиця кандидатів від `esc_billing_grade_cost`, руками | ні | ні |
| **прод / платні виклики** | платний запуск перекладу (59 викликів; оцінка скрипта ≤ $3,7, реально ≈ $0,1–0,6), його робить оператор; міграція на деплої створює порожню таблицю; спершу варто перевірити кодування (§11) | ні: відповіді записані. Живу перевірку поведінки v2 («нічого не підходить», частку «вигадок») краще зробити окремим платним кроком перед перемиканням, не в К5 | ні |
| **поведінка проду при `served_by: todays_mentor`** | не змінюється: збирач тексту працює лише на новому шляху; на старті перевіряється розмовник, тож усі 60 файлів мусять бути повними | не змінюється: подачі не доходять до стадій. Але старт перевіряє нову стадію, її виконавця й промпт v2 — це сторожує `test_file_passes_the_startup_checks` | не змінюється |
| **чи можна віддати хмарі** | код і тести — так (CI ганяє тести з базою); запуск перекладу — оператор локально (ключі провайдера, вимір через Usage) | так; залежить від адаптера пошуку інших доручень: К5 можна злити з портом і дублером у тестах, але продова фабрика мусить повертати справжній адаптер до перемикання | так |

**Відкриті питання до сесії «Ментор»:**

1. `project`: крок нема куди вставити, поки немає стадій оцінки й пояснення (§2).
2. URL матеріалу в порталі (§4).
3. `audio` як вид позиції (§4).
4. Чи писати `completeness = 1.0` разом з `authenticity` (§7).
5. Як обирати заміну при повторному пошуку (§5).
6. Фільтр ролі матеріалу й порядок курсу — передати авторам адаптера (§3).
