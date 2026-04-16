# Content Ingestion — карта документації та memory-файлів

**Призначення цього файлу:** єдина точка входу для роботи над шаром
"Контент і структура" (Content Ingestion). Тут зібрані посилання на
усі документи, memory-файли та дизайн-нотатки, які стосуються цього
шару. Файл живе у репо — щоб не зникав між сесіями.

**Статус на 2026-04-17 (кінець сесії 31):** PR #4c запушений, але
архітектурно некоректний. Ми зупинились на тому, що завтра користувач
напише **нову архітектуру даних цього шару з привʼязкою до моделі даних**,
а далі пройдемось блоками.

---

## 1. Канонічні документи (source of truth)

| Файл | Що там | Статус |
|---|---|---|
| `current-doc/db-schema.md` | Живий опис усіх 14 таблиць БД: Призначення / Поля / Індекси / FK / Інваріанти / Lifecycle. Part II — target-спец Content Ingestion. | ✅ Актуальний, оновлюється після кожної міграції |
| `current-doc/content-ingestion-INDEX.md` | **Цей файл** — мапа усього решти | ✅ |
| *(створюється завтра)* `current-doc/content-ingestion-architecture.md` | Нова дизайн-конституція шару — писатиме користувач | ⏳ TODO |

---

## 2. Ключові memory-файли (дизайн та правила)

Усі лежать у `~/.claude/projects/-Users-kostyantynzivenko-Desktop-documents-COURSE-SUPPORTER/memory/`.

### 2.1 Дизайн Content Ingestion

| Файл | Короткий зміст |
|---|---|
| `project_content_ingestion_design.md` | **Повний target-дизайн шару (сесія 30).** 23 рішення: таблиці (Entry/Macro/Segment), 3-stage pipeline (raw/macro/detail), per-source spec (text/web/video/audio/presentation), strategy pattern, soft-delete scope, API endpoints, 5-PR roadmap. Головний "що будуємо". |
| `project_content_structure_layering.md` | **Правило шарування (сесія 31).** Entry = metadata, Macro = план (без тексту), Segment.content = єдине місце тексту. Мета шару: отримати→розбити→підготувати. |
| `project_long_video_chunking.md` | Дизайн чанкінгу довгих відео (2h). Two-level (media 20-min / outline 10-min), три-step aggregation, ціль ~$0.07/video. Поглинається новою архітектурою як Stage 1 для video. |
| `project_chunked_large_output.md` | Як дробити широкий Architect/Methodist output на дешеві DeepSeek-чанки + merge. Related до long-video chunking. |
| `project_pr4c_segment_invariants.md` | Відкладене рішення: чи переводити Python-інваріанти сегментів у DB-constraints (btree_gist exclusion + trigger). Переглянути коли перший реальний Stage 6 writer landнеться. |

### 2.2 Правила поведінки (feedback)

| Файл | Правило |
|---|---|
| `feedback_reason_from_purpose.md` | **Перед кодом проти DB-entity спочатку сформулюй призначення таблиці.** Mechanical correctness ≠ architectural correctness. (Сесія 31 — корінь semantic flaw у PR #4c.) |
| `feedback_no_speculative_fields.md` | Не додавати "резервні" колонки під майбутнє. |
| `feedback_consistent_testing.md` | Одна модель для ВСІХ кроків у E2E. |
| `feedback_api_quota.md`, `feedback_no_waste_api.md` | API quota discipline, single foreground process. |
| `feedback_check_env_before_prod.md` | Перевірити keys в `.env.prod` перед prod-deploy. |
| `feedback_no_deploy.md` | Frontend тестуємо локально (не деплоїмо). |
| `feedback_mistral_structured.md` | Mistral structured-output nuances. |

### 2.3 Суміжні дизайни

| Файл | Зв'язок з Content Ingestion |
|---|---|
| `project_data_layers_architecture.md` | Стара (2026-04-07) картина Layers 1-2-3. **ЧАСТКОВО ЗАСТАРІЛО** — нова архітектура міняє Layer 1/2. Використовувати лише для історичного контексту. |
| `project_hw006_mentor_design.md` | Mentor agent (консьюмер Content Ingestion вихідних даних). |
| `project_homework_pipeline_plan.md` | Homework pipeline — консьюмер. |
| `project_model_routing_refactor.md`, `project_router_abstraction_refactor.md` | ModelRouter — інструмент Content Ingestion. |
| `project_vd_pipeline.md`, `project_vd017_outline.md` | VD (Visual Detection) — компонент Stage 1 для video/presentation. |
| `project_stt_plan.md`, `project_stt_guided_vd.md` | STT — компонент Stage 1 для video/audio. |

---

## 3. Історія сесій (state snapshots)

Читаються лише коли треба зрозуміти "як ми сюди прийшли". Не є дизайн-документами.

| Файл | Дата | Що тоді робили |
|---|---|---|
| `project_session31_state.md` | 2026-04-17 | **Остання сесія.** PR #4c запушений, user flagged architectural flaw. Резюме у pending A/B/C decision. |
| `project_session30_state.md` | 2026-04-15 | Shipped PR #380 (auth plan-based); **повний Content Ingestion target design** captured in db-schema.md Part II. |
| `project_session29_state.md` | 2026-04-14 | task_type shipped; 3 hotfixes; Methodist design rethink. |
| `project_session28_state.md` | 2026-04-13 | E2E close; long-video chunking design agreed. |
| `project_session27_state.md` | 2026-04-12..13 | Language pipeline end-to-end; DeepSeek/VD fixes. |
| `project_session26_state.md` | 2026-04-11 | Cost tracking; VD refactor; Instructor. |
| `project_session25_state.md` | 2026-04-10 | HW-007 + quality hardening + deploy. |
| `project_session24_state.md` | 2026-04-09 | HW-006 Mentor. |
| `project_session23_state.md` | 2026-04-08 | HW-001..005. |
| `project_session22_state.md` | 2026-04-07 | Data Layers + Methodist. |
| `project_session23_next.md`, `project_session24_next.md` | — | Стара плани наступних сесій (застаріле). |

---

## 4. `MEMORY.md` — навігаційний індекс

`~/.claude/projects/-Users-kostyantynzivenko-Desktop-documents-COURSE-SUPPORTER/memory/MEMORY.md` —
це **індекс** усіх memory-файлів з короткими описами (не сам зміст).
Завантажується автоматично на старті кожної сесії. Після того як завтра
напишемо `content-ingestion-architecture.md` — додам у MEMORY.md
one-liner-посилання на нього як resume-entry #1.

---

## 5. Код, на який впливає нова архітектура

| Файл | Роль | Чому важливо |
|---|---|---|
| `src/course_supporter/storage/orm.py` | ORM (14 таблиць) | Entry/Macro/Segment ORM-моделі. CHECK constraints, індекси. |
| `src/course_supporter/ingestion/base.py` | `MaterialProcessor` protocol | 3-stage контракт (process_raw / process_macro / process_detail). |
| `src/course_supporter/ingestion/{text,web,video,presentation}.py` | Per-source процесори | Нова стратегія — кожен пише `segments` з чистим текстом. |
| `src/course_supporter/ingestion/macro_segment_pipeline.py` | Pipeline (text/web) | Наразі семантично некоректний — читає JSON-blob з `processed_content`. |
| `src/course_supporter/ingestion_callback.py` | Пише `processed_content` JSON | Legacy writer — source of duplication. |
| `src/course_supporter/api/tasks.py::_collect_source_documents` | Генерація: читач `processed_content` | Головний read-consumer. Читає `SourceDocument.model_validate_json(...)`. |
| `src/course_supporter/fingerprint.py` | Merkle hash | Сьогодні бере `processed_content`. При міграції — потребує пересадки. |

---

## 6. План завтра (2026-04-18)

1. Користувач пише чернетку нової архітектури в `current-doc/content-ingestion-architecture.md` (структура: мета шару → per-entity spec → per-source strategy → legacy-to-remove → roadmap).
2. Я читаю цей файл **перед** будь-якою іншою дією.
3. Доопрацьовуємо блоками — per-block Q&A → commit у `content-ingestion-architecture.md`.
4. Коли документ закритий — розбираємо, що робити з PR #4c (A/B/C рішення) і плануємо PR #4d на основі нового документа.
5. Оновлюю `MEMORY.md` → ставлю `content-ingestion-architecture.md` як resume-entry #1.

---

## 7. Правила, які вже закріплені (не треба узгоджувати заново)

- **One point at a time** — обговорюємо блоками, не дампаємо багатосекційні specs.
- **Reason from table purpose first** — перед кодуванням проти entity завжди формулюємо його призначення.
- **Текст матеріалу живе ТІЛЬКИ в `material_segments.content`.** Entry = metadata, Macro = план без тексту.
- **DB-schema doc — living reference,** оновлюється одразу після міграції на main.
- **No speculative fields** — колонки "про запас" не додаємо.
- **Українська для обговорення, англійська — для ідентифікаторів та docstrings.**
