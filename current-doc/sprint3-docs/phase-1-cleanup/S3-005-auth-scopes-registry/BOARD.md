# S3-005: Auth Scopes Registry

**Тип:** Enhancement
**Пріоритет:** Low
**Складність:** S
**Phase:** 1

## Опис

Замінити hardcoded scope strings (`"prep"`, `"check"`) на центральний реєстр `config/auth.yaml` з Pydantic валідацією.

## Вплив

- Всі route файли (заміна string literals)
- Config system (новий YAML + Pydantic model)

## Definition of Done

- Scopes визначені в YAML та валідуються при старті
- Всі routes використовують references замість literals
