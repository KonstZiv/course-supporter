# S2-057: Flow Guide (Layer 1) — Деталі для виконавця

**Epic:** EPIC-7 — Integration Documentation + Manual QA

---

## Контекст

Перша задача epic. Створює концептуальний огляд системи — "карту" для нового користувача API. Не містить curl-прикладів (це Layer 2), не описує варіації параметрів (це Layer 3). Тільки "що" і "навіщо", без "як саме".

## Структура документа `docs/api/flow-guide.md`

### 1. What is Course Supporter?

- AI-powered система для трансформації навчальних матеріалів у структурований план курсу
- Приймає: відео, презентації, текстові документи, веб-посилання
- Видає: структурований курс (модулі → уроки → концепти → вправи)

### 2. Core Concepts

- **Course** — верхній рівень, контейнер
- **Material Tree** — ієрархія nodes для організації матеріалів (як файлова система)
- **Material Entry** — конкретний матеріал, прикріплений до node
- **Ingestion** — автоматична обробка матеріалу (транскрипція, OCR, scraping)
- **Slide-Video Mapping** — опціональна прив'язка слайдів до таймкодів відео
- **Structure Generation** — LLM-генерація структури курсу на основі оброблених матеріалів
- **Snapshot** — збережений результат генерації (версіонування)
- **Job** — асинхронна задача (ingestion або generation)

### 3. The Main Flow

Діаграма (mermaid або ASCII):

```
Create Course → Build Tree → Upload Materials → [Wait for Ingestion]
    → [Optional: Slide-Video Mappings] → Generate Structure
    → [Wait for Generation] → Get Result
```

Опис кожного кроку (1-2 речення):
- Що відбувається
- Який endpoint відповідає
- Що повертається

### 4. Supported Material Types

| Type | Formats | Processing |
|------|---------|------------|
| video | .mp4, .webm, .mkv, .avi | Transcription (Gemini → Whisper fallback) |
| presentation | .pdf, .pptx | Slide extraction + OCR |
| text | .md, .docx, .html, .txt | Content extraction |
| web | URL | Web scraping (trafilatura) |

### 5. Async Operations Pattern

- Upload/generate повертають `job_id`
- Polling: `GET /api/v1/jobs/{job_id}` до `complete` або `failed`
- Job statuses: `queued` → `active` → `complete` / `failed`

### 6. Authentication

- API Key в header `X-API-Key`
- Scopes: `prep` (read+write), `check` (read-only)
- Rate limits per scope
- Tenant isolation — кожен бачить тільки свої дані

### 7. What's Next?

Посилання на Quick Start (S2-058) і Endpoint Reference (S2-059).

---

## Checklist

- [ ] Текст написаний англійською (документація для зовнішніх користувачів)
- [ ] Mermaid-діаграма рендериться в mkdocs
- [ ] `mkdocs build --strict` проходить
- [ ] Узгоджено з автором проєкту
- [ ] Посилання на наступні документи додані

---

## Нотатки

_Простір для нотаток виконавця під час роботи над задачею._
