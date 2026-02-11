# S1-006: CI pipeline

## Мета

GitHub Actions pipeline на кожен PR: lint → type check → tests → AI code review. Зламаний код не потрапляє в main, кожен PR отримує автоматичний AI-ревʼю.

## Що робимо

1. **3 паралельні jobs**: `lint` (ruff check + format check), `typecheck` (mypy strict), `test` (pytest). Паралельність — швидше feedback, чіткіше видно що зламалось
2. **AI Code Review** ([ai-code-reviewer](https://github.com/KonstZiv/ai-code-reviewer)): запускається ТІЛЬКИ після успішного завершення всіх 3 jobs. Аналізує diff через Gemini, залишає inline-коментарі з рекомендаціями та "Apply suggestion" кнопками
3. **uv з кешуванням**: `astral-sh/setup-uv@v5` + `uv sync --frozen --group dev` (без media/whisper). Lockfile з Git гарантує відтворюваність
4. **Concurrency**: cancel-in-progress — при новому push в PR попередній run скасовується
5. **Branch protection**: require status checks для lint/typecheck/test (AI review — не required, залежить від зовнішнього API)
6. **Secrets**: `GOOGLE_API_KEY` для Gemini (free tier: 15 req/min). `GITHUB_TOKEN` — автоматичний
7. **README badge**: статус CI на головній сторінці репо

## Очікуваний результат

PR створено → lint + typecheck + tests паралельно → все зелене → AI review аналізує diff → inline-коментарі з рекомендаціями до PR. Загальний час < 5 хв.

## Контрольні точки

- [x] CI тригериться на PR до `main` та push в `main`
- [x] `Lint & Format` job: ruff check + ruff format --check проходять
- [x] `Type Check` job: mypy strict проходить
- [x] `Tests` job: pytest проходить з `ENVIRONMENT=testing`
- [x] `AI Code Review` job: запускається тільки після успіху 3 базових jobs
- [x] AI review залишає inline-коментарі до PR
- [x] AI review НЕ запускається на push в main (тільки PR)
- [ ] Pipeline завершується за < 5 хвилин
- [ ] При fail lint/typecheck/test — PR не можна merge
- [x] CI badge відображається в README

## Залежності

- **Блокується:** S1-001 (репо), S1-002 (ruff/mypy конфіг)
- **Блокує:** нічого напряму, але захищає main від регресій для всіх наступних задач
- **Secrets:** `GOOGLE_API_KEY` додати вручну в GitHub Settings

## Деталі

Повний spec (workflow YAML, secrets setup, branch protection, майбутні розширення): **T006-ci.md**
