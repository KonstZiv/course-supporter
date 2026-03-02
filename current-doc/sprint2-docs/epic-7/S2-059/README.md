# S2-059: Endpoint Reference (Layer 3)

**Epic:** EPIC-7 — Integration Documentation + Manual QA

---

## Мета

Повна довідка по кожному endpoint з варіаціями параметрів, edge cases, кодами помилок. Кожна варіація протестована на production.

## Що створюємо

Набір сторінок в `docs/api/reference/`, згрупованих за доменами:

| Файл | Endpoints | Кількість |
|------|-----------|-----------|
| `courses.md` | Courses CRUD | 3 |
| `nodes.md` | Material Tree (nodes) | 8 |
| `materials.md` | Material Entries + Legacy upload | 6 |
| `mappings.md` | Slide-Video Mappings | 3 |
| `generation.md` | Structure Generation | 4 |
| `jobs.md` | Job Status | 1 |
| `reports.md` | Cost Reports | 1 |
| `auth.md` | Authentication, scopes, rate limits | — |
| `errors.md` | Error codes, retry strategies | — |

## Формат опису кожного endpoint

```
### METHOD /path

**Description:** Що робить

**Auth:** Required. Scope: prep / prep,check

**Parameters:**
- path: ...
- query: ...
- body: ... (JSON schema)

**Request Example:**
curl ...

**Response (success):**
HTTP 2xx
{...}

**Response (error cases):**
- 400: ...
- 404: ...
- 422: ...
- 429: Rate limit (Retry-After header)

**Variations:**
- Варіація 1: опис + curl
- Варіація 2: опис + curl

**Notes:**
- Edge cases, gotchas, tips
```

## Як робимо — ітеративний процес

Для **кожної групи endpoints**:

1. Написати документацію всіх endpoints групи
2. Протестувати кожен endpoint з основними параметрами
3. Протестувати варіації та edge cases
4. Якщо знайдено баг — фікс → test → deploy → оновити документацію
5. Протестувати error responses (невалідні дані, відсутні ресурси, rate limit)
6. Фіналізувати документацію групи

Порядок груп: auth → courses → nodes → materials → mappings → generation → jobs → reports → errors

## Очікуваний результат

- 9 файлів документації з повним описом всіх 29 endpoints
- Кожен endpoint має curl-приклад, описані parameters, response schema
- Error codes документовані з retry стратегіями
- Всі варіації протестовані на production

## Тестування

**Автоматизовано:** `mkdocs build --strict`, OpenAPI schema validation

**Manual QA:** Кожен curl-приклад виконаний на production. Error cases перевірені.

## Залежності

**Попередня задача:** [S2-058: Quick Start](../S2-058/README.md) (happy path вже протестований)
