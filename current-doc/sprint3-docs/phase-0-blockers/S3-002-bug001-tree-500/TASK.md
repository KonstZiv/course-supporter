# S3-002: BUG-001 — GET /nodes/tree returns 500

**Phase:** 0 (Production Blockers)
**Складність:** S
**Статус:** INVESTIGATION STARTED

## Проблема

`GET /api/v1/courses/{course_id}/nodes/tree` повертає 500 Internal Server Error.

**Request:** `curl -s .../courses/019cb3ee-32b0-7602-a292-ce156a249e9d/nodes/tree -H "X-API-Key: ..."`
**Expected:** 200, tree JSON
**Got:** `{"detail":"Internal server error"}`
**Context:** Course має root + 2 children + 8 grandchildren. Всі nodes створені через POST без помилок.

## Попередній аналіз

Досліджено в попередній сесії:

1. **Route** (`api/routes/nodes.py:151-169`): `get_tree()` викликає `repo.get_tree(course_id)`, потім `NodeTreeResponse.model_validate(r)`.
2. **Repository** (`storage/material_node_repository.py:93-139`): `get_tree()` завантажує всі nodes одним запитом, збирає дерево в Python. Line 129: `node.children = []` скидає ORM relationship перед ручною збіркою.
3. **Schema** (`api/schemas.py`): `NodeTreeResponse` — recursive Pydantic model з `children: list[NodeTreeResponse]`.

**Можлива причина:** Lazy-loading issue — при Pydantic `model_validate()` рекурсивного дерева SQLAlchemy намагається lazy-load relationships, але session вже закрита або detached.

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `src/course_supporter/storage/material_node_repository.py` | Виправити `get_tree()` — можливо потрібен `selectinload` або конвертація в dict перед return |
| `src/course_supporter/api/routes/nodes.py` | Можливо потрібно змінити спосіб конвертації в response model |
| `tests/unit/test_api/test_node_routes.py` | Додати/оновити тест для tree endpoint з реальною ієрархією |

## Деталі реалізації

### Крок 1: Відтворити помилку

Запустити тести для tree endpoint, перевірити чи помилка відтворюється. Якщо ні — потрібен інтеграційний тест з реальною БД.

### Крок 2: Перевірити lazy-loading

Додати `selectinload(MaterialNode.children)` або `joinedload` в query, щоб дерево було повністю завантажене до закриття session.

### Крок 3: Перевірити конвертацію ORM → Pydantic

`NodeTreeResponse.model_validate()` з `from_attributes=True` на ORM об'єкті з рекурсивними relationships може тригерити lazy-load. Можливе рішення — конвертувати в dict/dataclass перед валідацією.

### Крок 4: Перевірити edge cases

- Порожнє дерево (тільки root без children)
- Глибоке дерево (3+ рівнів)
- Node без MaterialEntry

## Acceptance Criteria

- [ ] `GET /nodes/tree` повертає 200 з коректним JSON деревом
- [ ] Працює для дерева з 3+ рівнями (root → children → grandchildren)
- [ ] Тест покриває tree endpoint з реальною ієрархією
- [ ] На production: tree endpoint для existing course працює
