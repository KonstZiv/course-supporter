# S1-018: SourceMaterial Persistence (Repository)

## Мета

Реалізувати `SourceMaterialRepository` з CRUD-операціями та state machine для статусу обробки (pending → processing → done/error).

## Що робимо

1. **SourceMaterialRepository** — CRUD: create, get_by_id, get_by_course_id, update_status, delete
2. **Status machine** — валідація переходів (pending→processing, processing→done, processing→error)
3. **Side effects** — `done` → sets `processed_at`, `error` → sets `error_message`
4. **Unit-тести** — ~8 тестів з мокнутим `AsyncSession`

## Контрольні точки

- [ ] `create()` додає матеріал з правильним course_id
- [ ] `get_by_id()` → `SourceMaterial | None`
- [ ] `get_by_course_id()` → `list[SourceMaterial]`
- [ ] `update_status("processing")` — pending → processing OK
- [ ] `update_status("done")` — sets processed_at
- [ ] `update_status("error")` — sets error_message
- [ ] Invalid transition (pending → done) → `ValueError`
- [ ] `make check` проходить

## Залежності

- **Блокується:** немає (паралельно з S1-011)
- **Блокує:** немає
