# S3-005: Auth Scopes Registry — config/auth.yaml

**Phase:** 1 (Cleanup)
**Складність:** S
**Статус:** PENDING

## Контекст

API scopes (`"prep"`, `"check"`) захардкожені як string literals в route файлах (`courses.py`, `nodes.py`, `materials.py`, `generation.py`, `jobs.py`, `reports.py`). Немає центрального реєстру, немає документації що кожен scope дозволяє.

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `config/auth.yaml` | НОВИЙ — визначення scopes, descriptions, rate limits |
| `src/course_supporter/config.py` | Pydantic model для auth config, завантаження при старті |
| `src/course_supporter/api/routes/*.py` | Замінити hardcoded `"prep"` / `"check"` на references з registry |
| `src/course_supporter/api/deps.py` | Валідація scopes при API key перевірці |
| `tests/` | Тести для config loading, scope validation |

## Деталі реалізації

### 1. Config file (config/auth.yaml)

```yaml
scopes:
  prep:
    description: "Course preparation operations (CRUD, upload, generation)"
    rate_limit_field: rate_limit_prep
  check:
    description: "Course checking operations (read-only queries, mentoring)"
    rate_limit_field: rate_limit_check
```

### 2. Pydantic model (config.py)

```python
class ScopeConfig(BaseModel):
    description: str
    rate_limit_field: str

class AuthConfig(BaseModel):
    scopes: dict[str, ScopeConfig]
```

Завантаження аналогічно `models.yaml` → `external_services.yaml` pattern.

### 3. Routes

Замінити:
```python
# Було:
tenant = require_auth(request, scope="prep")

# Стало (варіант 1 — enum):
tenant = require_auth(request, scope=AuthScope.PREP)

# Або (варіант 2 — const):
tenant = require_auth(request, scope=SCOPE_PREP)
```

### 4. Validation

При створенні APIKey — валідувати scopes JSONB проти registry.

## Acceptance Criteria

- [ ] `config/auth.yaml` існує з визначенням всіх scopes
- [ ] Config завантажується та валідується при старті
- [ ] Hardcoded scope strings замінені на references
- [ ] APIKey scopes валідуються проти registry
- [ ] Тести покривають config loading та validation
