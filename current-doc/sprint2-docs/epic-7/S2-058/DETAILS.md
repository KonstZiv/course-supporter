# S2-058: Quick Start (Layer 2) — Деталі для виконавця

**Epic:** EPIC-7 — Integration Documentation + Manual QA

---

## Контекст

Друга задача epic. Це найважливіший документ — перше реальне "руками поторкати" для користувача API. Одночасно це manual QA happy path на production. Баги, знайдені тут, фіксяться одразу.

## Структура документа `docs/api/quick-start.md`

### Преамбула

- Що ми зробимо в цьому guide (створимо курс, завантажимо матеріал, отримаємо структуру)
- Prerequisites: API key, curl, доступ до API

### Крок 1: Verify Access

```bash
curl -H "X-API-Key: YOUR_KEY" https://api.pythoncourse.me/health
```

Очікуваний response: 200, `{"status": "ok", ...}`

### Крок 2: Create Course

```bash
curl -X POST https://api.pythoncourse.me/api/v1/courses \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"title": "My First Course"}'
```

Зберегти `course_id` з response.

### Крок 3: Create Root Node

```bash
curl -X POST https://api.pythoncourse.me/api/v1/courses/{course_id}/nodes \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"title": "Course Materials"}'
```

Зберегти `node_id`.

### Крок 4: Upload Material

```bash
curl -X POST https://api.pythoncourse.me/api/v1/courses/{course_id}/nodes/{node_id}/materials \
  -H "X-API-Key: YOUR_KEY" \
  -F "source_type=text" \
  -F "file=@sample.md"
```

Зберегти `entry_id` і `job_id`.

### Крок 5: Poll Job Status

```bash
curl https://api.pythoncourse.me/api/v1/jobs/{job_id} \
  -H "X-API-Key: YOUR_KEY"
```

Повторювати до `status: "complete"`. Описати polling pattern (інтервал, timeout).

### Крок 6: Verify Material Ready

```bash
curl https://api.pythoncourse.me/api/v1/courses/{course_id}/materials/{entry_id} \
  -H "X-API-Key: YOUR_KEY"
```

Перевірити `state: "ready"`.

### Крок 7: Generate Structure

```bash
curl -X POST https://api.pythoncourse.me/api/v1/courses/{course_id}/generate \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Зберегти `job_id` з response (status 202).

### Крок 8: Poll Generation Job

Аналогічно кроку 5.

### Крок 9: Get Generated Structure

```bash
curl https://api.pythoncourse.me/api/v1/courses/{course_id}/structure \
  -H "X-API-Key: YOUR_KEY"
```

Показати приклад відповіді з модулями, уроками, концептами.

### What's Next?

Посилання на Endpoint Reference для деталей і варіацій.

---

## Тест-матриця для Manual QA

Після написання основного happy path — протестувати варіації:

| Крок | Варіація | Що перевіряємо |
|------|----------|----------------|
| 4 | .md (маленький, <1KB) | Базовий text ingestion |
| 4 | .md (великий, >100KB) | Handling великих файлів |
| 4 | .docx | DOCX parsing |
| 4 | .pdf | PDF slide extraction |
| 4 | .pptx | PPTX slide extraction |
| 4 | .mp4 (коротке, <1min) | Video transcription |
| 4 | web URL | Web scraping |
| 4 | Невалідний файл | Error handling |
| 5 | Job failed | Error recovery |
| 7 | Матеріал не ready | 422 handling |
| 7 | Повторна генерація | Idempotency (200 vs 202) |

---

## Bug Tracking

Формат запису знайдених багів:

```
### BUG-001: [Короткий опис]
- **Крок:** N
- **Запит:** curl ...
- **Очікувано:** ...
- **Отримано:** ...
- **Причина:** ...
- **Фікс:** PR #NNN
- **Тест:** test_xxx.py::test_yyy
```

---

## Checklist

- [ ] Всі 9 кроків написані з curl-прикладами
- [ ] Кожен curl протестований на production
- [ ] Responses в документації відповідають реальним
- [ ] Варіації з тест-матриці протестовані
- [ ] Знайдені баги зафіксовані, виправлені, покриті тестами
- [ ] Повний end-to-end smoke test пройдений
- [ ] `mkdocs build --strict` проходить

---

## Нотатки

_Простір для нотаток виконавця під час роботи над задачею._
