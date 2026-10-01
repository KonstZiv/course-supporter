# PROBE-09 — зондування задачі 09 «вердикти по критеріях»

Гілка `probe/09-verdicts` від `main` @ `bc7e3f6` (після #585 і #588). Лише читання коду;
жодних викликів постачальників, ключів чи змінних середовища. SDK перевірено за
вихідним кодом пакетів, встановлених `uv sync` з `uv.lock` (openai 2.20.0,
anthropic 0.79.0, google-genai 1.62.0, dashscope 1.25.18, instructor 1.15.1).

Позначки: **Факт** — прочитано в коді/конфігу (з посиланням). **Припущення** — не
підтверджено кодом репозиторію (знання про API постачальника, тлумачення доручення).
«Не знайдено» — пошук по `src/`, `config/`, `prompts/`, `tests/`, `docs/` нічого не дав.

Шляхи без префікса — від `src/course_supporter/`.

---

## Коротко

1. Сьогодні **жоден конектор не просить у постачальника схему**: роутер кличе лише
   `provider.complete()` (`llm/stage_router.py:797`); `expects_json` дає JSON mode
   тільки Gemini (`llm/providers/gemini.py:229-230`), решта лише зрізає markdown-огорожу.
   Методи `complete_structured()` є в усіх п'яти конекторах, але **жодного виклику з
   продакшн-коду** (лише юніт-тести).
2. Модель у сьогоднішньому Ментора пише лише бали 0–100 по шарах + тексти; усе
   арифметичне (агрегат, денойз, вердикт) — код (`homework/review_scoring.py`).
   Вердиктів по критеріях немає ніде: ні в `ReviewResult`, ні в `ReviewStructureV1`,
   ні в колонках `homework_submissions`.
3. Новий шлях для `task`/`project` має лише стадії `safety`, `attempt_classifier`;
   стадії оцінки і збиральника результату для цих типів немає
   (`config/submission_paths.yaml:105-117`, `homework/result_builders.py:125-128`).
   Стадія не повертає даних тілу — лише `carry_on`/`ends_path`; свої вердикти вона
   пише в БД сама (зразок — `safety_result`, `sanity_result`).
4. Пара «перелік + ідентифікатор» уже закладена: `CriteriaInForce.source_id`
   (id рядка `task_criteria_lists` або `task_criteria_overrides`) + `Criterion.id`
   `c<N>` / точки `c<N>.p<M>` (`homework/criteria_list_service.py:156-171`,
   `homework/criteria_form.py:131-150`).
5. Побічна знахідка: `ceilings.output_tokens` стадій нового шляху **не доходить до
   дроту** для щаблів із `max_output_tokens: null`, і тому щабель
   `gemini-3.5-flash-lite` на `safety`/`attempt_classifier` за кодом завжди
   пропускається грошовою стелею (розд. 10, Р1).

---

## 1. Сьогоднішній Ментор: як народжуються вердикт і бал

**Факт.** Оркестратор — `MentorReviewService.review` (`homework/review_graph.py:130-285`),
кличеться з ARQ-задачі в стадії `review` (`api/tasks.py:1229-1243`), результат
пишеться `store_review_result(result=..., review_markdown=..., score=...)` одним UPDATE
(`storage/homework_repository.py:451-466`).

| Поле `ReviewResult` (`models/mentor_review.py:118-143`) | Хто пише | Де |
|---|---|---|
| `layers[].score`, `strengths`, `weaknesses` | **модель** (`LayerJudgment`, `agents/mentor_review.py:53-61`) | стадії `mentor_layered_evaluation_node_course` (1A) і `mentor_layered_evaluation_industry` (1B), паралельно через `asyncio.gather` (`review_graph.py:182-201`) |
| `layers[].weight` | **код** з `config/mentor_review.yaml` (`layer_weights` 0.5/0.3/0.2) | `review_graph.py:203-209`, `_layer` 288-295 |
| `aggregate_score` | **код** `aggregate_score()` — зважена сума, `floor(x+0.5)`, кламп 0..100 | `review_scoring.py:70-78`; виклик `review_graph.py:212` |
| `history_reconciliation.recidivism/corrections` | **модель** (`DenoisingOutcome`), код відкидає вигадані `prior_submission_id` | стадія `mentor_denoising`, `review_graph.py:215-234`, `_reconciliation_items` 297-317 |
| `history_reconciliation.denoise_delta`, `denoised_score` | модель пропонує `delta`, **код** клампить до `cap=10` і 0..100 | `review_scoring.py:81-89`; `review_graph.py:229-230` |
| `score_signals` (D9) | **код**: `magnitude/direction` = вага × (бал − 50); `reason` — текст `rationale` моделі | `review_scoring.py:110-150`; `review_graph.py:236-246` |
| `verdict.passed/correctness` | **код** за порогами `pass_score 60 / correct_min 85 / partially_correct_min 50` | `review_scoring.py:92-107`; `review_graph.py:258` |
| `criteria_unavailable` | **код** — код причини сервісу переліку | `review_graph.py:154-168` |

Синтез тексту (`mentor_synthesis`) отримує готові шари/сигнали і повертає markdown
(`agents/mentor_review.py:246-276`); на бал не впливає. Історія студента —
`_history`/`_within_depth`, глибина `MENTOR_HISTORY_TASK_DEPTH = 3`
(`review_graph.py:102, 350-421`).

**Де критерії входять у промпт.** Лише в 1A:
`get_or_compose()` → `criteria_pairs()` (`review_graph.py:154-156, 445-465`) робить пари
`{"statement": "[must|should|may] <text>", "evidence": "<evidence> — mandatory points: a; b"}`;
шаблон рендерить їх списком
(`prompts/mentor_layered_evaluation_node_course/v1.md:69-74`, «authoritative rubric»).
Ідентифікатори `c1…` і `check_method`, `soft_descent`, `concepts` у промпт **не
передаються**; точки — без своїх id. 1B (industry), денойз і синтез критеріїв не бачать.
Модель не повертає нічого про окремі критерії — лише два бали шарів
(`v1.md:42-55`).

**Факт.** Сьогоднішній Ментор кличе `get_or_compose` (може скласти перелік — це виклик
моделі), а не `load_in_force` (лише читання, без моделі) — `review_graph.py:154`.

## 2. «Трирівневий валідатор»

**Припущення щодо тлумачення.** Буквального «three-tier validator» у коді не знайдено.
Є два кандидати; описую обидва.

**(А) Валідатор трьох шарів D8** — `ReviewResult._exactly_three_distinct_layers`
(`models/mentor_review.py:145-155`): `layers` мусить містити рівно `node`, `course`,
`industry` по одному. Плюс межі на рівні типу: `Layer.weight` у (0, 1)
(`:49-53`), бали 0..100 (`:54, 85-89, 124-128`), `extra="forbid"` всюди.
- Викликається неявно при конструюванні `ReviewResult(...)` у `review_graph.py:248-260`
  — **після** всіх викликів моделі фаз 1 і 3, до синтезу.
- При відмові: `pydantic.ValidationError` летить з `review()` наверх — це не
  `StructuralRetryError`, повтору з поясненням чи спуску драбиною немає; задача
  падає. Практично недосяжно: шари складає код із фіксованих імен (`:203-209`).
- Хто ще вживає: імпорти `models.mentor_review` — лише `review_graph.py` і
  `review_scoring.py` (типи `Layer`, `LayerName`, `ScoreSignal`, `Verdict`,
  `Correctness`). Новий шлях **не вживає** `ReviewResult` зовсім — у нього
  `ReviewStructureV1`. Читачі збереженого JSONB `review_result["layers"]` —
  `_history` (`review_graph.py:370`); `review_result["verdict"]` — вебхук
  (`homework/webhook.py:101`) і портал (`api/routes/_portal_shared.py:152`).

**(Б) Трирівнева перевірка відповіді в агенті** — у кожної JSON-фази свій
`_validator` (`agents/mentor_review.py:140-148, 182-189, 215-232`):
1) розбір JSON + 2) pydantic-схема (`model_validate_json`, `extra="forbid"`, межі
балів) + 3) семантика (непорожні `rationale`/`summary`/`signal`/`note`). Будь-яка
відмова → `StructuralRetryError` з пояснюючим текстом (`:102-115`).
- Викликається роутером на кожній непорожній відповіді (`llm/stage_router.py:797-799`).
- При відмові: **один** повтор на тому ж щаблі з дописаним до user-промпту
  поясненням (`stage_router.py:685-697, 733-767`; `max_retries_structural=1`,
  `:269`) — це **друга оплата** (новий виклик з повним входом); якщо повтор теж
  невдалий — спуск на наступний щабель (**третя оплата**). Повтор, що впав, дає
  причину `"STRUCTURAL: retry exhausted"` без `empty_at_ceiling`, тож навіть зі
  `stop_on_output_ceiling=True` спуск продовжується (`:756-757`, `:576-585`).
- Той самий механізм (`response_validator` + `StructuralRetryError`) вживають усі
  агенти: `sanity.py`, `criteria_decomposer.py`, `key_explainer.py`, `methodist.py`,
  `security/stage2.py`, інгестія (`ingestion/*.py`) — 18 викликів з `expects_json=True`.
  Тобто механізм спільний; конкретні три валідатори 1A/1B/денойз — лише Ментора.

## 3. Новий шлях: кістяк, структура рецензії, збиральник

**Кістяк (задача 03).** `run_new_path_if_switched` (`homework/path_runner.py:114-189`)
→ `_run_path` (`:224-366`): двері (безкоштовно) → запит фондів → цикл стадій
(`:295-334`) → `reviewing` → `ResultBuilder.build` → `store_review_result`
(`:338-355`) → доставка → `after_delivery`.
- Стадія = опис у `config/submission_paths.yaml` + виконавець у реєстрі
  (`homework/path_stages.py:121-160`); старт відмовляє, якщо стадія без виконавця
  (`validate_stage_executors`, `:149-167`).
- Виконавець отримує `StageContext` (`:67-91`) і повертає лише
  `StageOutcome(carry_on, terminal_status, reason_code)` (`:94-117`) — **даних
  тілу не повертає**. Свій результат пише сам у БД: `store_safety_result`
  (`path_stages.py:230-232`), `store_sanity_result` (`:291-293`).
- Виклик моделі зі стадії — через `StageExecution` (`llm/stage_router.py:199-243`),
  зібраний `_execution()` (`path_stages.py:170-199`): `stop_on_output_ceiling=True`,
  `money_ceiling_usd=ceilings.money_usd`.
- Контрольні точки: `PathCheckpoint` (`homework/path_checkpoint.py:136-159`) зберігає
  лише стан стадій `pending|done`, причину заморозки, лічильник повторів. **Виходів
  стадій не зберігає.** Продовження пропускає `done`-стадії
  (`path_runner.py:294-299`), тож стадія, чий вихід потрібен збиральнику, мусить
  мати його в БД.
- Кінець драбини: `LadderStop.EXHAUSTED` → повтор задачі `arq.Retry` до ліміту;
  `OUTPUT_CEILING`/`MONEY_CEILING` → заморозка без повтору
  (`path_runner.py:565-648`, `path_checkpoint.py:111-115`).
- **Де стала б стадія оцінки — не знайдено.** Для `task`/`project`: `first: [safety,
  attempt_classifier]`, повтори `[safety]`, `served_by: todays_mentor`
  (`config/submission_paths.yaml:105-117`); `short_task` — `paths: {}` (`:101-103`).
  Збиральник є лише для `TEST` (`homework/result_builders.py:125-128`); для інших
  типів `builder is None` → рецензія не пишеться (`path_runner.py:347`).
- Поле `PathStage.deterministic` (`homework/path_config.py:163`) обов'язкове в
  конфігу, але **кодом не читається** (grep `.deterministic` — лише оголошення).

**Структура рецензії (задачі 01 + 04).** `VersionedReview.schema_version: Literal["1"]`
(`models/review_schema.py:40-59`); тіло — `ReviewStructureV1`
(`models/review_structure.py:316-406`): `verdict{passed, why}`, `test`, `fixed`,
`new_remarks`, `open`, `broken` (списки `Remark{what, why, todo, read, position}`),
`mentor_voice`, `replies`, `verification{by_run, by_reading}`, `progress`;
порядок — `SECTION_ORDER` (`:61-72`).
- **Полів вердиктів по критеріях немає.** Бал для не-тесту в структурі ніде не
  живе; є лише `TestSection.score` для тесту (`:298`). Бал іде окремо —
  `BuiltResult.score` → колонка `score` (`result_builders.py:74-84`).
- `Verdict.why` обов'язковий, крім тесту (`review_structure.py:381-396`).
- Додавання поля, яке читач мусить знати = нова версія схеми
  (`review_schema.py:21-26`).

**Збиральник (задача 04).** `assemble_review` (`homework/review_assembler.py:265`) —
чистий, текст лише з розмовника; з оцінки чекає заповнену `ReviewStructureV1`
(вердикт + зауваги). Про критерії/вердикти нічого не знає.

**Хто читає бал і вердикт нового шляху.** `version_1_outcome`
(`api/routes/_portal_shared.py:101-125`): `passed` — з `structure.verdict`,
`correctness` — з колонки `score` за правилом тесту (100 → correct, 0 → incorrect,
між — partially; `homework/test_scoring.py:179-190`). Докстрінг прямо каже: для не-тесту
це правило «only until task 09, which revisits it» (`_portal_shared.py:118-119`).
Вебхук читає те саме (`homework/webhook.py:94-98`).

## 4. Маршрутизатор і конектори: структурований вивід

**Шляхи виклику (факт).** Два входи — `execute_for_stage` (за ім'ям, 21 виклик) і
`execute_stage` (з готовим `StageConfig`; для нового шляху через `StageExecution`) —
сходяться в одну прогулянку драбиною (`stage_router.py:346-587`) →
`_attempt_entry` → `_call_with_log` → **`provider.complete(request)`**
(`:797`). Один шлях виклику постачальника. `LLMRequest` (`llm/schemas.py:11-33`)
не має поля схеми; є лише `expects_json: bool`.

Можливі точки для гілки «зі схемою / без» (місця, не рекомендація):
`LLMRequest` (`schemas.py:11-33`), `_build_request` (`stage_router.py:619-661`),
сигнатури `execute_for_stage`/`execute_stage`/`StageExecution.run`, кожен
`provider.complete`, а також `attempt_input_document` (`llm/input_hash.py:65`) —
його докстрінг вимагає додати туди кожне поле, що впливає на вихід.

| Провайдер (ladder `provider`) | Клас / SDK | Що сьогодні йде на дріт при `expects_json=True` | `complete_structured` (не викликається) | Що вміє SDK з `uv.lock` (за кодом пакета) |
|---|---|---|---|---|
| `gemini` | `GeminiProvider`, google-genai 1.62.0 | `response_mime_type="application/json"` (JSON mode **без схеми**) + зрізання огорожі (`gemini.py:229-245`) | `response_mime_type` + `response_schema=<pydantic>` (`:259-298`) | `GenerateContentConfig.response_schema` і `response_json_schema` (`google/genai/types.py:3928-3929`); є `seed` (`:5127`) |
| `deepseek` | `DeepSeekProvider` ← `OpenAICompatProvider`, openai 2.20.0; `extra_body.thinking=disabled` (`deepseek.py:47-51`) | лише зрізання огорожі (`openai_compat.py:225-237`), **без `response_format`** | instructor `create_with_completion` (режим за замовчуванням — tool calling), `max_retries=2` (`openai_compat.py:248-306`) | SDK має `response_format` (json_object / json_schema) і `seed` (`openai/resources/chat/completions/completions.py:108, 207`) |
| `deepseek_thinking` | `DeepSeekThinkingProvider` ← той самий клас, thinking on за замовчуванням (`deepseek_thinking.py`) | те саме | те саме | те саме SDK |
| `mistral` | `OpenAICompatProvider` з `base_url` Mistral (`llm/factory.py:61-65`) | те саме | те саме | те саме SDK |
| `dashscope` | `DashScopeProvider`, нативний dashscope 1.25.18 (`AioGeneration` / `AioMultiModalConversation`) | лише зрізання огорожі (`dashscope.py:521-522`) | схема вписується в system-промпт (`:551-578`) | `Generation.call(**kwargs)` пропускає будь-які kwargs у `parameters` (`dashscope/aigc/generation.py:205-252`); `response_format`/`seed` у докстрінгу SDK **не згадані** |
| `anthropic` (немає в драбинах Ментора) | `AnthropicProvider`, anthropic 0.79.0 | лише зрізання огорожі (`anthropic.py:86-117`) | схема в system-промпт (`:119-139`) | `messages.create(output_config=...)` з `JSONOutputFormatParam{type:"json_schema", schema}` (`anthropic/resources/messages/messages.py:110`; `types/json_output_format_param.py:11-15`); `tool.strict` |
| `openai` (немає в драбинах Ментора) | `OpenAICompatProvider` | як deepseek | як deepseek | як deepseek |

**Факт.** Ознака `structured_output` — це лише декларація в реєстрі
(`config/external_services.yaml`, `llm/registry.py:22`), яку стартова перевірка
звіряє з `requires` стадії (`llm/ladder_config.py:270-274`,
`homework/path_config.py:230-234`). На поведінку на дроті вона не впливає.

**Припущення (не з коду, з публічних знань про API; перевірити окремо):**
DeepSeek приймає `response_format={"type":"json_object"}`, але не `json_schema`;
строгі tool-calls у DeepSeek — бета; поведінка з thinking on невідома. Mistral API
приймає `json_schema`. Qwen на DashScope приймає `response_format` json_object
(json_schema — залежить від моделі/регіону). Код репозиторію цього не підтверджує.

**Як відповідь розбирається.** Конектор повертає текст (`LLMResponse.content`);
розбір і перевірка — у `response_validator` агента через
`Model.model_validate_json` (`agents/mentor_review.py:140-148` тощо).
`LLMProvider._parse_structured` (`providers/base.py:129-163`) кидає
`StructuredOutputError`, але досяжний лише з `complete_structured`.

## 5. Обрізана відповідь

**Розпізнавання стелі (факт).** Кожен конектор нормалізує свій сирий код у
`FinishReason` (`llm/finish_reason.py:36-78`):

| Конектор | Сире значення «стеля» | Де |
|---|---|---|
| OpenAI-compat (openai/deepseek/deepseek_thinking/mistral) | `choice.finish_reason == "length"` | `openai_compat.py:43-50, 244` |
| Gemini | `Candidate.finish_reason == MAX_TOKENS` | `gemini.py:29-31, 255` |
| Anthropic | `stop_reason == "max_tokens"` | `anthropic.py:29-30, 111-115` |
| DashScope | `finish_reason == "length"` (`output.finish_reason` або `choices[0]`) | `dashscope.py:110-112, 430-450` |

`finish_reason` пишеться в рядок `ExternalServiceCall` кожної спроби
(`stage_router.py:863`).

**Порожня відповідь на стелі.** `_attempt_entry` позначає `empty_at_ceiling`
(`stage_router.py:719-727`), реєстр — `outcome=EMPTY_AT_OUTPUT_CEILING`
(`:192-195`). Шлях за ім'ям (сьогоднішній Ментор) — спуск далі; шлях нового
Ментора (`stop_on_output_ceiling=True`) — зупинка `LadderStop.OUTPUT_CEILING`
(`:576-585`) → заморозка ревізії `FreezeReason.OUTPUT_CEILING` без повтору
(`path_runner.py:620-623`).

**Непорожнє обрізане тіло (факт).** Роутер обрізання окремо не перевіряє: тіло
віддається валідатору. Обрізаний JSON не розбирається → `StructuralRetryError` →
повтор на тому ж щаблі (друга оплата) → спуск (третя). Реєстр: `outcome=
INVALID_CONTENT` + `finish_reason=output_ceiling`, тож випадок видно в даних
(`stage_router.py:176-177, 190-191`). Без валідатора (наприклад, синтез) обрізаний
текст пройде як успіх. Повтор не збільшує `max_tokens` — той самий запит + дописане
пояснення.

**Обрізаний JSON під примусом схеми (факт із SDK + припущення).**
- openai SDK: хелпер `.parse()` кидає `LengthFinishReasonError` при
  `finish_reason="length"` (`openai/lib/_parsing/_completions.py:100`). У
  `OpenAICompatProvider.classify_error` цього типу немає → `SEMANTIC` → негайний
  спуск, **без** структурного повтору (`openai_compat.py:157-175`). Сирий
  `chat.completions.create` з `response_format` не кидає — поверне обрізане тіло.
- instructor (якщо вживати `complete_structured`): `IncompleteOutputException` при
  обрізанні (`instructor/processing/function_calls.py:48-52`); конектор перетворює
  лише `InstructorRetryException` на `StructuredOutputError`
  (`openai_compat.py:280-288`), решта → `classify_error` → `SEMANTIC`.
- Gemini з `response_schema`: тіло повертається як є з `MAX_TOKENS`;
  `_parse_structured` кине `StructuredOutputError` → у роутері `SEMANTIC`.
- **Припущення:** примус схеми не рятує від обрізання — постачальник обриває
  генерацію на стелі незалежно від граматики; результат — невалідний JSON з
  `finish_reason=length/MAX_TOKENS`.

## 6. Драбини стадій оцінки

**Сьогоднішній Ментор** (`config/ladders_mentor.yaml`), усі `requires:
[structured_output]` крім синтезу, `input_budget_ratio: 0.5`, `record_output` не
задано → `false` (`llm/ladder_config.py:99`):

| Стадія | Щабель | Провайдер / модель | `max_output_tokens` | Реєстр: capability / max_output | Схема на дроті сьогодні |
|---|---|---|---|---|---|
| `mentor_layered_evaluation_node_course` (`:236-265`) | 1 | deepseek_thinking / deepseek-v4-pro | 32768 | structured_output / 65536 | ні |
| | 2 | dashscope / qwen3.7-max | 8192 | structured_output / 65536 | ні |
| | 3 | deepseek / deepseek-flash | 8192 | structured_output / 384000 | ні |
| `mentor_layered_evaluation_industry` (`:271-291`) | 1–3 | як 1A | 32768 / 8192 / 8192 | як вище | ні |
| `mentor_denoising` (`:296-309`) | 1 | dashscope / qwen3.7-max | 8192 | | ні |
| | 2 | gemini / gemini-3.8-flash | 8192 | structured_output / 65536 | JSON mode без схеми |
| | 3 | deepseek / deepseek-flash | 8192 | | ні |
| `mentor_synthesis` (`:315-328`) | 1–3 | як денойз | 8192 | `requires: []` | — (текст) |
| `criteria_decomposition` (`:143-183`) | 1/2/3 | deepseek_thinking v4-pro / qwen3.7-max / deepseek-flash | 32768 / 16384 / 16384 | | ні |

**Новий шлях** (`config/submission_paths.yaml`): стадій оцінки **немає**. Наявні
`safety`, `attempt_classifier` (`:21-87`): mistral-small-latest (null) /
deepseek-flash (8192) / gemini-3.5-flash-lite (null), `ceilings.output_tokens: 8192`,
`money_usd: 0.05`.

Спостереження з коментарів конфігу (факт про зафіксоване, не перевірено мною):
1A на щаблі 1 вже раз упиралась у 8192 з порожнім тілом (`ladders_mentor.yaml:241-256`);
перелік на той момент — 37 критеріїв (`:250-252`). Щаблі 2–3 оцінки мають 8192.

## 7. Критерії як вхід

**`load_in_force`** (`homework/criteria_list_service.py:255-270`):
`async def load_in_force(session: AsyncSession, authored_document_id: uuid.UUID) ->
CriteriaInForce | None` — читає документ і два живі рядки, застосовує
`choose_in_force` (`:213-252`), без виклику моделі; `None`, якщо документ видалено
або чинного переліку немає (ще не складено, `pending`, застаріла версія).
Складання на промаху — лише `CriteriaListService.get_or_compose` (`:385-434`),
який повертає `CriteriaInForce | CriteriaUnavailable(reason)`.

**`CriteriaInForce`** (`:156-171`): `criteria: tuple[Criterion, ...]`, `layer:
AUTHOR|MODEL`, `source_id: uuid.UUID`. Докстрінг: «A verdict names a criterion by
this pair (decision 21): identifiers are stable within one list, not across the
lists of one version».

**Сталий ідентифікатор переліку в БД (факт).**
- `task_criteria_lists.id` (UUIDv7, `storage/orm.py:2597-2700`); один живий рядок на
  завдання (`uq_task_criteria_lists_authored_document_id_active`); старі версії —
  soft-deleted історія. Нова версія / одноразове перескладання (decision 13) —
  новий рядок (`release` старого, `criteria_list_service.py:482-520`).
- `task_criteria_overrides.id` (`orm.py:2781-2830`); правка автора замінює цілим
  новим рядком, попередній soft-deleted (`:2782-2786`).
- Отже `source_id` змінюється з кожною правкою/перескладанням; старі рядки лишаються
  (soft-delete), тож пара «`source_id` + `c<N>`» розв'язна й пізніше. Таблиці
  розрізняються — у пари потрібен ще `layer`, щоб знати, яку таблицю читати
  (UUIDv7 у двох таблицях формально не перетинаються, але тип джерела з id не видно).

**Ідентифікатори критеріїв і точок (факт).** `Criterion.id` — `^c[1-9][0-9]*$`,
`MandatoryPoint.id` — `^c[1-9][0-9]*\.p[1-9][0-9]*$` (`criteria_form.py:149-150`);
присвоює код (`criterion_id`, `point_id`, `:131-146`; `compose_criteria`, `:409`).
Правка автора зберігає наявні id і дає новим наступний номер після найбільшого в
обох шарах (`criteria_edit_service.py:316-430`). Вага — категорія `must|should|may`,
числа 3/2/1 рахує код (`WEIGHT_NUMBERS`, `criteria_form.py:91-94`;
`Criterion.weight_number`, `:220-223`). `soft_descent` ⇔ `check_method ==
code_test` (`:226-236`).

## 8. Детермінованість

**Факт.**
- `temperature`: `LLMRequest.temperature = 0.0` за замовчуванням (`llm/schemas.py:17`);
  ні роутер, ні конфіг драбин її не задають (`LadderEntry` має лише `provider, model,
  reasoning, max_output_tokens` — `ladder_config.py:52-55`). Усі конектори
  передають її на дріт (`openai_compat.py:228`, `gemini.py:225`, `anthropic.py:92`,
  `dashscope.py:486-487`).
- `seed`: **не знайдено** ніде в `src/`. SDK openai і google-genai його підтримують.
- `deepseek_thinking` — thinking on (щабель 1 обох оцінок). **Припущення:** у режимі
  міркування temperature постачальником ігнорується, вихід недетермінований.
- `qwen3.7-max` — `reasoning: null` → `enable_thinking` не передається, режим моделі
  за замовчуванням (`dashscope.py:222-250`).
- Вхід спроби хешується (`input_hash`, разом із temperature, max_tokens, reasoning,
  provider) і пишеться в реєстр (`stage_router.py:866`) — інструмент «той самий
  вхід?». Вихід моделі в реєстр не пишеться (`record_output=false` на всіх стадіях
  Ментора).
- Код підрахунку (`review_scoring.py`) чистий: без годинника, випадковості, БД;
  округлення `floor(x+0.5)`. `asyncio.gather` не впливає на значення.

**Джерела різних балів на «тому ж вході» (факт із коду):**
- спуск драбиною залежить від стану постачальників → інша модель відповідає;
- `get_or_compose` може скласти/перескласти перелік (decision 13) між двома
  поданнями → інший `source_id` і зміст переліку;
- історія студента (денойз) росте з кожним поданням — вхід фази 3 змінюється;
- ротація ключів (`itertools.cycle`) на вихід не впливає.

## 9. Тести-замки

| Що | Тести | Що зламається, якщо… |
|---|---|---|
| `ReviewResult` D8 + межі | `tests/unit/test_mentor_review_contract.py` (12 тестів: `test_missing_layer_rejected`, `test_duplicate_layer_rejected`, `test_extra_fourth_layer_rejected`, ваги, межі, `extra`) | …валідатор (А) зняти з `ReviewResult` — 3 тести шарів; зняти «на новому шляху» — **нічого**, новий шлях `ReviewResult` не вживає |
| Агент фаз | `tests/unit/test_agents/test_mentor_review_agent.py` (13: invalid JSON → structural retry, blank rationale, extra field, score range) | …змінити `_validator` (Б) або моделі `LayerJudgment`/`NodeCourseEvaluation` |
| Граф | `tests/integration/test_review_graph.py` (8), `test_homework_pipeline.py` (5), `test_one_door_db.py` (4), `test_project_submission_worker.py` (5), `test_criteria_road_e2e_db.py` (3) | …змінити форму `ReviewResult`/`criteria_pairs`/порядок фаз |
| Підрахунок | `tests/unit/test_review_scoring.py` (15) | …змінити формули/округлення |
| Промпти | `tests/unit/test_prompts/test_mentor_review_prompts.py` (7), `test_prompt_slot_lock.py` (13), `test_prompt_json_locks.py` | …змінити шаблон 1A / слоти |
| Драбини | `tests/unit/test_llm/test_ladder_config.py` (74; закріплено 32768 на щаблі 1 — `:375-392`), `tests/unit/test_homework/test_submission_paths_file.py` | …змінити щаблі/стелі |
| Роутер | `tests/unit/test_llm/test_stage_router.py` (57; структурний повтор `:1156-1279`, порожня відповідь `:390, 530`, стеля `:806-853`, гроші `:951-1040`, поля запиту `:1323-1412`), `test_stage_router_register.py` (13), `tests/integration/test_stage_router_db.py` (5) | …змінити гілки повтору/спуску чи поля `LLMRequest` |
| Хеш входу | `tests/unit/test_llm/test_input_hash.py` (`test_every_output_affecting_part_changes_the_hash`) | нічого не зламається автоматично: тест перелічує поля вручну (`:44-53`), тож нове поле схеми в `LLMRequest`, не додане в `attempt_input_document`, пройде непоміченим — потрібен новий рядок параметризації |
| Конектори | `test_providers.py`, `test_providers_dashscope.py`, `test_providers_deepseek*.py` (у т.ч. `kwargs == {"extra_body": ...}`, `test_providers_deepseek.py:88`), `test_json_extract.py:72-79` (`response_mime_type`), `test_provider_classifiers.py` | …змінити kwargs `create`/`GenerateContentConfig` |
| Структура рецензії | `tests/unit/test_review_schema.py` (6), `test_review_structure.py` + doctest-и `models/review_structure.py`, знімки збиральника | …додати поля в `ReviewStructureV1` без нової версії |
| Новий шлях | `tests/integration/test_new_path_pipeline.py` (9), `test_path_e2e_stages_db.py` (10), `tests/unit/test_homework/test_path_stages.py` | …змінити `StageOutcome`/реєстр/`_execution` |

## 10. Ризики, несподіванки, обсяг

**Несподіванки (факт).**
- **Р1. Стеля виводу стадії шляху не доходить до дроту.** `_execution` передає
  `rung.max_output_tokens` як є (`path_stages.py:189`); для `null` роутер бере
  реєстрову стелю (`stage_router.py:647-659`), а не `ceilings.output_tokens`
  (той лише обмежує закріплені щаблі на старті, `path_config.py:309-315`).
  Коментар `submission_paths.yaml:33-35` стверджує протилежне. Наслідок для грошей:
  оцінка спроби = вхід + `tokens_out` реєстру (`stage_router.py:613-617`). Розрахунок
  через реєстр (без мережі): `gemini-3.5-flash-lite`, 1000 вх. + 65536 вих. =
  **$0.164 > $0.05** → щабель 3 `safety`/`attempt_classifier` пропускається
  грошовою стелею завжди, без виклику. Те саме станеться зі стадією оцінки з
  `null`-щаблями.
- **Р2.** `complete_structured` — мертвий у продакшні код у 5 конекторах; в
  OpenAI-compat він іде через instructor з власними `max_retries=2` (приховані
  додаткові оплати поза реєстром роутера) і власною класифікацією помилок.
- **Р3.** Новий шлях не має каналу «вихід стадії → збиральник»; вердикти треба десь
  зберігати (колонки/таблиці для них немає → міграція, якщо так вирішать).
- **Р4.** `version_1_outcome` для не-тесту рахує `correctness` за правилом тесту
  (100/0/між) — явно позначено «до задачі 09».
- **Р5.** Обсяг виводу: 37 критеріїв × (вердикт + слід-цитата) при стелі 8192 на
  щаблях 2–3 — ризик обрізання; обрізане непорожнє тіло → 2–3 оплати (розд. 5).
- **Р6.** Ментор сьогодні кличе `get_or_compose`, а доручення називає `load_in_force`
  (без складання → `None` на промаху). Рішення, хто складає перелік на новому шляху,
  не прийнято в коді.
- **Р7.** `deterministic: true` у конфігу шляху ні на що не впливає (не читається).
- **Р8.** Підтримка `json_schema` у DeepSeek (обидва режими) і DashScope/qwen3.7-max
  з коду не встановлюється; лише Gemini (і Anthropic/OpenAI, яких у драбинах Ментора
  немає) мають схему в SDK типізовано. Щабель 1 оцінок — `deepseek_thinking`.
- **Р9.** Структурний повтор не підвищує стелю — при обрізанні повтор майже
  гарантовано обріжеться знову (друга оплата марна).

**Оцінка обсягу частин (припущення, грубо).**

| Частина | Обсяг |
|---|---|
| Поле схеми в `LLMRequest` + гілка в роутері + `input_hash` + тести роутера | S |
| Примус схеми в конекторах: Gemini (`response_schema`) | S |
| …OpenAI-compat (`response_format` json_schema / tool) для mistral, deepseek, deepseek_thinking — з перевіркою, що приймає кожен постачальник | M |
| …DashScope (`response_format` через kwargs) — невідомо, чи приймає сервіс | M (з ризиком L, якщо треба фолбек на tool/промпт) |
| Класифікація обрізання під схемою (`LengthFinishReasonError` тощо) у `classify_error` | S |
| Модель вердиктів (pydantic) зі слідом «`source_id`/`layer` + `c<N>`/`c<N>.p<M>` + цитата» + формула 3/2/1 і «зараховано» в коді + юніт-тести ручного підрахунку | S–M |
| Стадія оцінки нового шляху: виконавець, промпт v2 з id критеріїв, запис вердиктів у БД (міграція), збиральник для `task`/`project`, поля в `ReviewStructureV1` (нова версія схеми?) | L |
| Перегляд `version_1_outcome` для не-тесту | S |
| Тести «на всіх конекторах структура за схемою» (записані відповіді) + «двічі той самий вхід → той самий бал» | M |
| Виправлення Р1 (стеля стадії → дріт) | S |
