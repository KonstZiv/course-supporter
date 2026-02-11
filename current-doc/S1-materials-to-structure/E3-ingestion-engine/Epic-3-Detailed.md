# Epic 3: Ingestion Engine

## Мета

Обробка всіх типів матеріалів курсу. Після цього епіку — система може прийняти відео, PDF/PPTX, текст (MD/DOCX/HTML) або URL і перетворити кожне джерело на уніфікований `SourceDocument`. Кілька джерел об'єднуються в `CourseContext`, готовий для Architect Agent.

## Що робимо

Чотири процесори + merge + persistence:

1. **SourceProcessor ABC** (S1-011) — базовий інтерфейс `async def process(source_url) -> SourceDocument`. Pydantic-моделі `SourceDocument`, `ContentChunk` для уніфікованого представлення.
2. **VideoProcessor primary** (S1-012) — Gemini 2.5 Flash Vision: завантаження відео через File API → таймкодований транскрипт зі structured output. Головний метод ingestion для відеоматеріалів.
3. **VideoProcessor fallback** (S1-013) — FFmpeg → сегменти → Whisper v3 STT → об'єднання. Автоматичне переключення при помилці Gemini.
4. **PresentationProcessor** (S1-014) — PDF (PyMuPDF) та PPTX (python-pptx): витягування тексту, рендеринг слайдів у зображення, Vision LLM для діаграм/графіків.
5. **TextProcessor** (S1-015) — MD, DOCX, HTML → plain text з чанкуванням по структурі документа (заголовки, параграфи).
6. **WebProcessor** (S1-016) — Fetch HTML → trafilatura → content extraction → snapshot збереження + URL preservation.
7. **MergeStep** (S1-017) — об'єднання кількох `SourceDocument` у `CourseContext`. Інтеграція `SlideVideoMapping` (ручний маппінг слайдів до таймкодів).
8. **SourceMaterial persistence** (S1-018) — CRUD для `source_materials`: статус-машина (pending → processing → done/error), збереження метаданих.

## Для чого

Ingestion — це "вхідна воронка" системи. Якість роботи Architect Agent (Epic 4) напряму залежить від якості витягнутого контенту. Кожен процесор оптимізований для свого типу матеріалу, а MergeStep створює повний контекст із cross-references між відео-таймкодами, слайдами та текстом.

## Контрольні точки

- [ ] VideoProcessor (primary): відео → таймкодований транскрипт через Gemini Vision
- [ ] VideoProcessor (fallback): те саме через FFmpeg + Whisper при помилці Gemini
- [ ] PresentationProcessor: PDF/PPTX → текст + slide images → Vision LLM для діаграм
- [ ] TextProcessor: MD/DOCX/HTML → chunked plain text
- [ ] WebProcessor: URL → extracted content + snapshot
- [ ] MergeStep: кілька SourceDocument → CourseContext з cross-references
- [ ] SourceMaterial CRUD: статус-машина працює коректно
- [ ] `make check` проходить

## Залежності

- **Блокується:** Epic 1 (DB, config), Epic 2 (ModelRouter для Vision LLM)
- **Блокує:** Epic 4 (ArchitectAgent потребує CourseContext)
- **Частковий паралелізм:** S1-011 (інтерфейс) + S1-015/S1-016 (найпростіші процесори) можна починати одразу. S1-012/S1-013 (відео) — найскладніші.

## Задачі

| ID | Назва | Естімейт | Примітка |
|:---|:---|:---|:---|
| S1-011 | SourceProcessor інтерфейс | 0.25 дня | ABC + Pydantic schemas |
| S1-012 | VideoProcessor (primary) | 0.5 дня | Gemini Vision, найважливіший |
| S1-013 | VideoProcessor (fallback) | 0.5 дня | FFmpeg + Whisper |
| S1-014 | PresentationProcessor | 0.5 дня | PDF + PPTX + Vision LLM |
| S1-015 | TextProcessor | 0.25 дня | Найпростіший процесор |
| S1-016 | WebProcessor | 0.25 дня | trafilatura |
| S1-017 | MergeStep | 0.5 дня | cross-references logic |
| S1-018 | SourceMaterial persistence | 0.25 дня | CRUD + status machine |

**Загалом: 3–4 дні** (найбільший епік спрінту)

## Ризики

- **Gemini File API** нестабільний для великих відео → fallback готовий (S1-013)
- **PPTX з нестандартним форматуванням** → фокус на стандартних, edge cases в backlog
- **Web scraping** може блокуватись сайтами → trafilatura як найнадійніший варіант, graceful degradation при помилці
