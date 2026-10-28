# Зондування задач 10 і 12 (перезбирання Ментора)

Лише читання коду на `main` @ `0648350`. Жодних змін у коді, тестах, конфігурації; жодних викликів моделей і зовнішніх служб.
Позначки: **Факт** (файл:рядок), **Припущення**, **Не знайдено** (де шукав). Шляхи `src/…` скорочено до `course_supporter/…` → `cs/…`.

## Коротко

1. **Пошук за концепцією є і ніким не вживаний**: `DocumentSegmentRepository.search_by_concepts` (`cs/storage/document_segment_repository.py:261`) — точний JSONB `@>` (І-семантика), лише активні сегменти, порядок курсу, **без жодної міри релевантності**; у `src/` викликів нема, лише інтеграційні тести.
2. **Якорі всіх чотирьох видів уже в даних** (`time`/`slide`/`paragraph`/`file`, `orm.py:909-956`) і вже проєктуються в `ConceptSearchHit.anchor`; «текст позиції» — `DocumentSegment.title`/`description` (nullable, для аудіо/відео часто NULL) або `content` (opt-in).
3. **Два розриви в обгортці**: (а) пошук не фільтрує `material_role`/`task_type`/`test_object` — може повернути методичний матеріал (еталонні розв'язки), який студентові заборонено бачити (`_portal_shared.py:77-98`); (б) концепції критеріїв беруться з `NodeSummaryFinal.main_concepts` у «переможному» написанні після `dedupe_concepts`, а пошук — точний збіг рядка, без `normalization_key` → можливі промахи.
4. **«Застарілість»**: сегменти лише м'яко видаляються (`deleted_at`), а **кожна переобробка документа видаляє старі й створює нові `id`** навіть за незмінного змісту (`orm.py:630-640`, `3677-3692`). Посилання за `segment_id` «застаріє» і від переобробки — варто вирішити, чи це «застарілість», чи повторне розв'язання.
5. **Структура рецензії вже має місце для довідок**: `Remark.read: list[Reference]` і `Remark.position` (`review_structure.py:155-162`); але `Reference` = лише `title`+`url`, без якоря, тексту позиції й id сегмента; `PositionKind` каже `"video"`, а `SegmentAnchor` — `"time"`. URL-схеми «матеріал + якір» у бекенді нема (є лише `GET /portal/materials/{id}`).
6. **Стадії 09б**: модель **не** повертає жодних ідентифікаторів курсу; `CriterionRemark` навмисно без поля читання («links … resolved by code (task 10)», `verdict_explanation.py:263-270`). Метрика `authenticity` у реєстрі є, але `record_review_metrics` у продакшн-коді **ніхто не кличе**.
7. **Ревізія = окремий рядок `homework_submissions`**; номера ревізії й версії завдання на подачі **нема**. Порядок ревізій — `(created_at, id)`; версію можна вивести лише непрямо через адресу переліку у вердиктах (`criteria_source_id` → `source_content_hash`).
8. **`c<N>` стабільні лише в межах одного переліку** (явно задокументовано): новий текст завдання → новий перелік із нумерацією з `c1`; навіть у межах версії можливе одноразове перескладання. Рядки вердиктів **видаляються й вставляються наново** при повторі стадії — їхні `id` нестабільні, посилатися з таблиці зауважень треба на `(submission_id, item_id)`.
9. **Розбіжність із брифом задачі 12**: код і ORM задокументували інший план реплік — «нові члени переліків у `student_feedback`, не нові таблиці й маршрути» (`feedback_kinds.py:18-30`, `orm.py:2027-2034`); бриф каже «три таблиці». Потрібне рішення до початку робіт.
10. **Ризики паралельної роботи**: єдина лінійна голова alembic (`submission_awaiting_criteria`) — дві міграції з тим самим `down_revision` дадуть дві голови й зламають `alembic upgrade head` у CI; спільні гарячі файли обох задач — `verdict_explanation.py`, `review_explainer.py`, `prompts/review_explanation/v1.md`, `text_result.py`, `review_structure.py`, 60 файлів розмовника. Оцінка: **10 — M**, **12 — L**.

---

## Задача 10 — довідки з курсу кодом

### 1. Пошук за концепцією

- **Факт.** Код: `cs/storage/document_segment_repository.py:261-401`, метод `DocumentSegmentRepository.search_by_concepts(*, course_root_id, concepts, include_secondary=False, include_content=False) -> list[ConceptSearchHit]`.
- **Факт.** Вихід — `ConceptSearchHit` (`:78-105`): `segment_id`, `document_summary_id`, `authored_document_id`, `course_node_id`, `node_title`, `material_title` (= `DocumentSummary.title`), `filename`, `source_type`, `anchor: SegmentAnchor`, `main_concepts`, `secondary_concepts`, `char_count`, `content | None`.
- **Факт.** Семантика: І-збіг усіх термінів через JSONB `@>` (`:311-316`); порожній список → `[]` (`:308-309`); область курсу — обов'язковий `course_root_id` (`:360`); лише `DocumentSegment.deleted_at IS NULL` (`:361-365`), предки окремо не перевіряються — покладається на каскад; порядок — порядок читання курсу (`:368-373`); **«No relevance score»** (`:305-306`).
- **Факт.** Збіг — точний рядок, «no query expansion» (`:287-290`); відомий ліміт: терміни, розщеплені між `main` і `secondary` одного сегмента, не знаходяться (`:291-295`).
- **Факт.** Викликачів у `src/` нема: `grep search_by_concepts|ConceptSearchHit|SegmentAnchor` по `src/` дає лише сам файл і коментар `orm.py:809`; вживають лише `tests/integration/test_document_segment_repository.py:596-903`.
- **Факт.** Пошук **не фільтрує** `AuthoredDocument.material_role` (`orm.py:424-434`), `task_type` (`:435-448`) чи `source_type='test_object'` (`:411-423`), а також `DocumentSummary.status` (`:715-726`). Портал ховає від студента все, крім `educational` (`cs/api/routes/_portal_shared.py:77-98`).
- **Факт.** Звідки концепції на критерії: `Criterion.concepts` (`cs/homework/criteria_form.py:203`); при складанні лишаються лише ті, що були у вході, у написанні входу (`:368-394`, `:421-426`); вхід — `main_concepts` `NodeSummaryFinal` вузла завдання і кореня (`cs/homework/criteria_edit_service.py:476-489`, `:662-663`). Вони — алгоритмічне об'єднання концепцій документів/дітей через `dedupe_concepts` (`cs/agents/methodist.py:965-998`), що обирає написання «голосуванням» (`cs/concept_dedup.py:1-23`).
- **Припущення.** Через п. вище концепція критерію може не збігтися посимвольно з написанням у конкретному сегменті → `@>` промахнеться. Обгортці варто шукати за `normalization_key` (або розширювати запит варіантами написання), а не точним рядком.
- **Не знайдено** готової міри впевненості (шукав `score|relevance|confidence` у `storage/`, `homework/`). **Припущення** — що вже є в даних, щоб обчислити її кодом без моделі:
  - частка концепцій критерію, покритих сегментом (запит по одній концепції + підрахунок; сьогодні лише І-збіг);
  - `main` проти `secondary` (`orm.py:874-886`);
  - близькість у дереві: `course_node_id` хіта vs вузол завдання (`HomeworkSubmission.node_id`, `orm.py:1498-1502`), предки/нащадки через `CourseNode`;
  - «пояснено раніше за завдання»: `CourseNode.order` / `AuthoredDocument.order` (порядок уже в `ORDER BY`);
  - `is_auxiliary` (`orm.py:887-897`) — допоміжний код-сегмент слабший;
  - розмір `content_char_count` (`orm.py:957-961`).

### 2. Матеріали курсу: документи, сегменти, концепції, якорі

- **Факт.** Ланцюг: `CourseNode` → `AuthoredDocument` (`orm.py:356`) → 1:1 активний `DocumentSummary` (`:630-669`, частковий унікальний індекс `:659-664`) → `DocumentSegment[]` (`:771`). `course_root_id` денормалізовано на кожному рівні (`:403-410`, `:681-687`, `:839-845`).
- **Факт.** Концепції: `main_concepts`/`secondary_concepts` JSONB на `DocumentSummary` (`:698-709`) і `DocumentSegment` (`:874-886`), GIN `jsonb_path_ops` (`:811-822`); на вузлах — `NodeSummaryRaw/Final.main_concepts` (`:2526-2535`, `:2682-2688`, Final — read-only копія Raw, `:2622-2623`).
- **Факт.** «Вказівник» як окрема сутність не зберігається: зв'язок концепція → сегмент існує лише як запит `@>` у момент вжитку (див. п. 1). **Не знайдено** таблиці вказівників (шукав `pointer|reference` в `orm.py`; `task_references` — це еталон тесту, `reference_kinds.py:1-23`).
- **Факт.** Якорі (`orm.py:909-956`): `start/end_time_sec` (відео/аудіо), `start/end_slide` (презентація), `start/end_paragraph` (текст/веб; може бути NULL — «KD-C empty anchor»), `file_path` (код). Відображення `source_type → kind` — `document_segment_repository.py:108-130` (явні гілки; новий тип → `ValueError`).
- **Факт.** Текст позиції: `DocumentSegment.title` (≤128, NULL для аудіо/відео, `:858-863`), `description` (NULL для медіа до Pass-2c, `:864-869`), `content` (`:870-873`), `DocumentSummary.title` (`:688-691`). Слайди мають рендери `AuthoredDocument.slide_keys` (`:495-507`).
- **Факт.** Форматування часу `mm:ss` є лише всередині відеоконвеєра (`cs/ingestion/video_pipeline/steps.py:266`); спільного форматера для `Position.value` **не знайдено**.

### 3. Вилучення й версії

- **Факт.** Усі чотири сутності — `SoftDeleteMixin` (`orm.py:91-125`); каскад м'якого видалення `AuthoredDocument → DocumentSummary → DocumentSegment` (`:3664-3670`).
- **Факт.** Переобробка документа м'яко видаляє старий summary з сегментами й вставляє новий (`orm.py:637-639`; `cs/storage/document_summary_repository.py:127-134`) — **нові `segment_id`**, старі лишаються історією без скрабу вмісту (`orm.py:3677-3692`).
- **Факт.** Жорсткого видалення сегментів у коді **не знайдено** (шукав `delete(DocumentSegment|DocumentSummary`, `purge`, `hard_delete`); лише `ON DELETE CASCADE` від `course_nodes`/`authored_documents` (`orm.py:834-845`).
- **Факт.** Версії на сегментах нема; є `content_hash` (Merkle, `orm.py:967-972`), якорі в нього навмисно не входять (`:914-916`).
- **Припущення.** «Застарілість» будувати на `segment.deleted_at IS NOT NULL`. Але через переобробку з новими `id` слід розрізнити: (а) документ/сегмент справді вилучено — посилання знімається; (б) документ переоброблено — можна спершу спробувати повторно розв'язати концепції (вони ж у критерії), і лише на невдачі — «застарілість». Рішення — за каноном.

### 4. Місце в новому шляху

- **Факт.** `criteria_evaluation`: вхід `EvaluationInput` — назва/опис/текст завдання, `criteria` (з концепціями), `submission_text`, мова (`cs/homework/criteria_evaluation.py:152-159`; `cs/agents/criteria_evaluator.py:66-87`). Вихід моделі — `EvaluationAnswer.verdicts[]` з `id`, `verdict`, `quote`, `missing` (`cs/homework/criteria_verdicts.py:112-145`); код перевіряє «рівно очікувані id» (`:166-221`) — **готовий шаблон перевірки «вигадки»**. Пише рядки `submission_criterion_verdicts` (`criteria_evaluation.py:177-191`).
- **Факт.** `review_explanation`: читає рядки вердиктів і перелік за їхньою адресою (`cs/homework/review_explanation.py:110-121`, `:157-190`), будує `ExplanationFacts` (`cs/homework/verdict_explanation.py:156-168`), вхід моделі — `render_context` (`cs/agents/review_explainer.py:129-142`): завдання, `result`, `criteria` з вердиктами, робота, мова. **Концепції критерію в промпт не передаються** (`review_explainer.py:145-167`). Вихід — `ExplanationAnswer{passed, why, remarks[{id, what, why, todo}], mentor_voice}` (`verdict_explanation.py:263-312`), перевірка проти фактів (`:315-389`), зберігається в `submission_explanations.body` (`review_explanation.py:139-144`).
- **Факт.** Збирач `TextResultBuilder` кладе зауваження в `new_remarks` як `Remark(what, why, todo)` — **без `read` і `position`** (`cs/homework/text_result.py:103-115`); перебудовується при повторному вході з `reviewing` (`cs/homework/path_runner.py:409-433`).
- **Припущення — куди вставити.** Чисто кодова довідка не потребує моделі: розв'язання «невиконаний критерій → концепції → сегменти» природно робити (а) у збирачі (`text_result.py`) — детерміновано з `Criterion.concepts`, або (б) окремим кроком після `review_explanation`, зі збереженням результату (щоб повторна збірка не змінила довідки). Перевірка «вигадки» має сенс, лише якщо моделі **показують** кандидатів (`segment_id` у вході `review_explanation`) і вона їх обирає/коментує — тоді код звіряє повернуті id з множиною показаних, як `read_answer` звіряє `c<N>`. Якщо обирає лише код, «вигадка» вироджується в перевірку власного коду. Вибір варіанта — рішення до плану.
- **Факт.** Запит до пошуку має знати корінь курсу: `HomeworkSubmission.course_node_id` — «Root CourseNode representing the course» (`orm.py:1493-1497`).

### 5. Що вже є в структурі рецензії й реєстрі

- **Факт.** `REVIEW_SCHEMA_VERSION = "1"`, `ReviewSchemaVersion = Literal["1"]` (`cs/models/review_schema.py:41-44`).
- **Факт.** `Remark.read: list[Reference]`, `Remark.position: Position | None` (`cs/models/review_structure.py:155-162`); `Reference{title, url}`, «resolved to a link by code» (`:121-132`); `Position{kind, value}`, «a position that no longer resolves is a stale reference (§2.12), handled where references are» (`:96-118`); `PositionKind = "video"|"slide"|"paragraph"|"file"` (`:84`).
- **Факт.** Асемблер друкує `position` і `read` як `[title](url)` (`cs/homework/review_assembler.py:133-144`); фрази `position.*`, `remark.read` є в розмовнику (`config/phrasebook/ukr.yaml:53`, `:65-69`), файлів мов — 60.
- **Факт.** Розбіжності форми: `Position` належить `Remark`, не `Reference` (одна позиція на зауваження за кількох посилань); `SegmentAnchor.kind="time"` vs `PositionKind="video"`; в `Reference` нема `segment_id`/якоря/тексту позиції, отже «застарілість» на читанні неможливо перевірити зі збереженої структури.
- **Факт.** Реєстр викликів: `authenticity` («share of claims supported by a reference») і `completeness` (`orm.py:1319-1328`), калькулятор `cs/review_metrics.py:1-60`, запис `record_review_metrics` (`cs/service_logging.py:316-340`). Викликів у продакшн-коді **не знайдено** — лише `tests/integration/test_review_metrics_db.py`.
- **Факт.** Обидві стадії `record_output: false` (`config/submission_paths.yaml`, блоки `criteria_evaluation`, `review_explanation`); у реєстрі зберігаються `prompt_ref`, `prompt_hash`, `input_hash` (`orm.py:1249-1261`).
- **Не знайдено** URL-схеми «матеріал + якір» (шукав маршрути `api/routes/portal_*.py`): є лише `GET /portal/materials/{authored_document_id}` (`cs/api/routes/portal_materials.py:137-176`). Як будувати `Reference.url` (глибоке посилання порталу) — у фронтенді, якого в цьому репозиторії нема.

---

## Задача 12 — гілка зауважень, розмова, репліки

### 6. Подачі й спроби

- **Факт.** Кожна спроба — окремий `HomeworkSubmission` (у коді — «revision», напр. `cs/homework/path_runner.py:4-6`); зв'язок спроб — лише пара `(student_id, authored_document_id)` (`orm.py:1490-1511`); окремі індекси на кожну колонку, складеного немає.
- **Факт.** Номера ревізії **нема** (шукав `revision` в `orm.py` — лише `test_versions`). Порядок задає `(created_at, id)` (`cs/storage/submission_verdict_repository.py:179-187`; `cs/storage/homework_repository.py:312-335`).
- **Факт.** Версії тексту завдання на подачі **нема**. Версія завдання = `AuthoredDocument.content_hash` + `task_type` (`orm.py:487-492`); перелік прив'язаний до неї через `source_content_hash`/`source_task_type` (`orm.py:3014-3023`, `:3166-3176`), «earlier versions are soft-deleted history» (`:2950-2954`). На подачі версія видна лише непрямо — через `criteria_source_id` у вердиктах.
- **Факт.** Повторність сьогодні: `has_reviewed_revision` — чи є живa подача в `completed|delivered`; нова версія тексту **не** робить подачу знову першою (`homework_repository.py:190-240`).
- **Факт.** Дедуплікація лише для ідентичного файлу (`find_duplicate`, `homework_repository.py:152-188`); обмежень на паралельні спроби того самого студента **не знайдено** (шукав `FOR UPDATE|advisory|skip_locked` — є лише в `project_base_repository.py:42`, `task_reference_repository.py:244`).

### 7. Вердикти й зіставлення між ревізіями

- **Факт.** Рядок на (подача, пункт), адреса = `criteria_layer` + `criteria_source_id` + `item_id` (`orm.py:2130-2158`, унікальний `(submission_id, item_id)` `:2226-2231`).
- **Факт.** **«Identifiers are stable within one list, not across the lists of one task version»** (`orm.py:2139-2145`; `submission_verdict_repository.py:160-161`; `criteria_list_service.py:154-165`).
- **Факт.** Складання нумерує з `c1` позиційно (`criteria_form.py:405-407`, `:422-424`) → у новій версії `c3` може бути іншим критерієм. Правка автора зберігає id базового переліку й не перевидає id вилучених (`criteria_edit_service.py:503-604`, зокрема `:579-585`), але дозволяє змінити будь-яке поле критерію (`criteria_form.py:280-292`). В межах версії модельний перелік може бути перескладено один раз (`criteria_list_service.py:509-515`).
- **Факт.** Запобіжник повторної подачі вже читає «останній ранній вердикт того самого переліку» (`submission_verdict_repository.py:146-191`) — це готовий запит «попередній стан пункту» для задачі 12, але лише в межах однієї адреси.
- **Факт.** `replace_for_submission` видаляє й вставляє рядки при кожному запуску стадії (`submission_verdict_repository.py:95-129`) → `SubmissionCriterionVerdict.id` нестабільний.
- **Припущення.** Однозначне зіставлення між ревізіями можливе: (а) та сама адреса переліку — за `item_id`; (б) правка автора над модельним переліком тієї ж версії — за збереженим `item_id` (як твердження автора); (в) нова версія — **неможливе кодом**; зауваження старої версії треба закривати окремим станом («перелік змінився»), а не виводити з вердикту.

### 8. Репліки сьогодні

- **Факт.** `student_feedback` — лише «дотик» без тексту: `kind='touch'`, `value ∈ {helped, not_helped}`, `text` завжди NULL, одна строка на (ціль, студент) (`orm.py:2000-2110`; `cs/feedback_kinds.py:38-70`). Маршрути: `POST /portal/submissions/{id}/feedback`, `POST /feedback/submissions/{id}` (`cs/api/routes/portal_submissions.py:648`, `cs/api/routes/feedback.py:53`), спільне ядро `cs/homework/feedback_core.py`.
- **Факт.** Задокументований план реплік — **у тій самій таблиці**: «a reply in text — arrives as MEMBERS of these three enums, not as new routes and not as new tables (§2.14)»; міграція звузить унікальність до дотиків і зробить `value` nullable (`feedback_kinds.py:18-30`; `orm.py:2027-2034`; `text` «is here because a reply is the same record», `:2091-2097`). **Розбіжність із брифом** («три таблиці») — потребує рішення.
- **Факт.** Єдиний текст студента — `student_note` (D7-local) до рецензії (`orm.py:1602-1606`); у новому шляху його бачить лише стадія `safety` (`cs/homework/path_stages.py:268-275`); `path_selection.py:82` прямо: «A student's free-text note is not a reply».
- **Факт.** Вхідних вебхуків з текстом студента **не знайдено** (`api/routes/homework.py`: лише `PATCH /tenant/webhook` для налаштування, `:435`).
- **Факт.** Механізм «позначеного блоку входу» є: слоти промпту блокуються від закриття тегу текстом студента (`cs/llm/prompt_loader_md.py:39-45`, `:106-134`), замки — `tests/unit/test_llm/test_prompt_slot_lock.py:226`, `:264`. Місце для відповіді у структурі є: `Reply{kind, said, answer}`, `ReplyKind = question|objection|comment`, розділ `replies` (`review_structure.py:89`, `:165-176`, `:363`), фрази `reply.*` у розмовнику (`ukr.yaml:59-63`).

### 9. Стан подачі

- **Факт.** Обчислюється в `resolve_submission_state` (`cs/homework/path_selection.py:66-90`): `REPEAT_WITH_REPLIES` «never returned … decision rather than omission … until task 12» (`:79-83`); перелік станів `cs/homework/path_config.py:92-102`; стан записується в чекпойнт і не перераховується на продовженні (`path_selection.py:16-18`).
- **Факт.** Конфіг уже має всі три шляхи для `task`/`short_task` з однаковими стадіями (`config/submission_paths.yaml`, блок `task_types`), коментар «until then both repeat states list the same stages — a prepared place».
- **Припущення.** Щоб стан став досяжним: (1) записи реплік; (2) правило «є репліка студента на зауваження цього завдання, ще не віддана в рецензію» (прив'язка репліки до ревізії, у якій на неї відповіли); (3) відповідна гілка в `resolve_submission_state`; (4) стадії шляху `repeat_with_replies`, що реально читають репліки (сьогодні той самий набір, отже — новий `prompt_ref`/стадія або версія промпту `review_explanation`).

### 10. Міграції

- **Факт.** Пишуться руками, autogenerate не вживається (`DD-L4-A`, ~200 операцій дрейфу) — `migrations/versions/submission_awaiting_criteria.py:19-21`, `submission_verdict_tables.py:41-46`. Словники CHECK — літералами в міграції, коментарі копіюються з ORM (`submission_verdict_tables.py:30-34`, `:58-64`). Голова: `submission_awaiting_criteria` (жоден файл не має її як `down_revision`).
- **Факт.** Тести: `tests/integration/test_schema_sync.py` (таблиці/колонки ORM ↔ БД), пер-задачні `test_*_schema_db.py` (коментарі колонок/таблиць, кожен CHECK відмовляє рівно свій рядок) — `tests/integration/test_submission_verdict_schema_db.py:1-13`, `:67-322`. CI: `alembic upgrade head`, потім `pytest --run-db --run-redis` (`.github/workflows/ci.yaml:75-82`).
- **Не знайдено** тестів `downgrade` і перевірки «одна голова» (шукав `downgrade|alembic.command|get_heads` у `tests/`).
- **Факт.** `CLAUDE.md` описує `make migrate` (autogenerate), що суперечить правилу «лише руками» з міграцій; `Makefile:38`.

---

## Для обох задач

### 11. Замки, тести, ризики

Тести, що впадуть або потребують свідомого оновлення:

| Зміна | Замок / тест |
|---|---|
| `REPEAT_WITH_REPLIES` стає досяжним | `tests/integration/test_path_selection_db.py:378-417` («never from today's data»); `tests/unit/test_homework/test_path_config.py`, `test_submission_paths_file.py` |
| Нові поля `Reference`/`Remark`/`Position`, новий розділ | `tests/unit/test_review_structure.py:113-139` (`SECTION_ORDER`, поля), `tests/unit/test_review_assembler.py:379` |
| Нова фраза розмовника | стартова перевірка набору мов (`cs/phrasebook/validation.py`) — 60 файлів `config/phrasebook/*.yaml` |
| Зміна промпту `review_explanation` (слот реплік, нове поле) | `tests/unit/test_llm/test_prompt_slot_lock.py:264`, `tests/unit/test_agents/test_review_explainer.py`, `tests/unit/test_homework/test_verdict_explanation.py`, `test_text_result.py`, `tests/integration/test_text_review_e2e_db.py` |
| Фільтри в `search_by_concepts` | `tests/integration/test_document_segment_repository.py:596-903` |
| Нові таблиці / колонки | `test_schema_sync.py`; якщо додаються до каскаду — `tests/storage/test_cascade_declarations.py:31-80` |
| Репліки як члени `FeedbackKind` | `ck_student_feedback_*`, `uq_student_feedback_target` (`orm.py:2038-2053`) і тести задачі 05 |

Ризики при паралельній роботі двох робітників (людей/агентів):
- **Факт.** Лінійна історія alembic з однією головою; дві гілки з `down_revision = "submission_awaiting_criteria"` → дві голови → `alembic upgrade head` у CI падає. **Припущення:** задача 10 може обійтися без міграції (довідки в JSON рецензії/пояснення); якщо ні — домовитися про порядок і перебазування `down_revision`.
- **Припущення.** Спільні гарячі файли: `verdict_explanation.py` (`CriterionRemark`/`ExplanationAnswer`), `review_explainer.py`, `prompts/review_explanation/v1.md` (обидві задачі, ймовірно, захочуть `v2`), `text_result.py`, `review_structure.py`, `config/submission_paths.yaml` (`prompt_ref`), розмовник. Варто наперед розподілити: 10 — `Remark.read`/`position`, 12 — `fixed/open/broken/replies` + слот реплік; версію промпту робить одна задача.

Ризики при кількох робітниках-процесах (рантайм):
- **Факт.** Кілька процесів/`max_jobs` (`cs/worker.py:245-308`; `docker-compose.prod.yaml:62-71`), серіалізації спроб одного студента нема (п. 6). **Припущення:** дві ревізії в польоті одночасно — стан зауважень, виведений «від попередньої ревізії», залежить від того, чи попередня вже має вердикти (`previous_verdicts` бачить лише записані). Потрібне правило: стан обчислюється від останньої **завершеної** ревізії, а запис стану — ідемпотентний і в транзакції чекпойнта (як вердикти, `criteria_evaluation.py:11-15`).
- **Припущення.** Номер ревізії, присвоєний при вставці, потребує унікального індексу `(student_id, authored_document_id, revision_number)` і повтору при конфлікті (дві одночасні подачі).
- **Факт.** Повторна стадія видаляє/вставляє вердикти (п. 7) — зовнішні ключі на `submission_criterion_verdicts.id` зламаються.

### 12. Оцінка й коміти

**Задача 10 — M (3–5 днів).** Багато готового (пошук, якорі, місце в структурі, фрази), основне — рішення про форму `Reference` і момент розв'язання.
1. Фільтри видимості в пошуку (`educational`, не завдання, не `test_object`, summary `ready`) + інтеграційні тести.
2. Обгортка `CourseReferences` (Protocol + реалізація): стабільний інтерфейс, пошук за `normalization_key`, міра впевненості — чиста функція з юніт-тестами.
3. Форма довідки: `Reference` + якір/текст позиції/`segment_id` (рішення щодо `schema_version`), узгодження `time`↔`video`, асемблер, фрази.
4. Розв'язання для невиконаних критеріїв (у збирачі або окремим кроком зі збереженням) + перевірка «вигадки» (якщо модель бачить кандидатів).
5. «Застарілість»: перевірка `deleted_at` при збиранні/читанні, зняття посилання, лог/лічильник + тести трьох критеріїв готовності.
6. (опційно) Запис `authenticity` у реєстр через `record_review_metrics`.

**Задача 12 — L (тиждень+).** Нові таблиці, ручні міграції з тестами схеми, конкуренція ревізій, нова версія промпту, вхідні маршрути з текстом студента.
1. Рішення «три таблиці vs члени `student_feedback`» (див. Коротко, п. 9) — до коду.
2. Міграція + ORM: розмова (студент×завдання), зауваження (розмова × ідентичність критерію, стан), репліки (текст, відповідь, ревізії) + `test_*_schema_db.py`.
3. Номер ревізії й версія завдання на подачі: міграція колонок, правило бекфілу, запис у `submission_core` з унікальним індексом.
4. Чиста функція «вердикт(попередній), вердикт(поточний), зміна переліку → стан» + юніт-тести інваріанту.
5. Оновлення станів після `criteria_evaluation` у тій самій транзакції, ідемпотентно; заповнення `fixed/open/broken` у збирачі.
6. Приймання реплік: маршрути порталу й ключа клієнта через спільне ядро (як `feedback_core`), ліміти й екран дверей (як `student_note`).
7. Промпт `review_explanation` v2: слот `<student_replies>` із замком слоту, поле відповіді у виході, перевірка «кожна репліка має рівно одну відповідь»; `replies` у рецензії.
8. `resolve_submission_state` → `REPEAT_WITH_REPLIES` + оновлення замка `test_path_selection_db.py:417` і коментарів конфігу.

## Чого не видно з коду

- Канон (`../refactoring-vision/vision.md`, `../CLAUDE.md`, `03-BINDING.md`, `TASK.md`, `PRE-FLIGHT.md`) у контейнері відсутній — **Не знайдено** (шукав `find . -name …` і `ls ..`); посилання на §2.12–§2.14 узяті з коментарів коду.
- Фронтенд порталу (схема глибоких посилань) — поза цим репозиторієм.
