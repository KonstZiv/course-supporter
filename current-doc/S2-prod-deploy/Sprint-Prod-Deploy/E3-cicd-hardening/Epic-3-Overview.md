# Epic 3: CI/CD & Hardening

## Мета

Automated deploy через GitHub Actions, security hardening, production logging, документація, smoke test. Після епіку — push to main автоматично деплоїть, production захищений, є документація для onboarding.

## Задачі

| ID | Назва | Залежності |
| :---- | :---- | :---- |
| PD-016 | GitHub Actions deploy workflow | Epic 2 deployed |
| PD-017 | Security hardening | PD-010 |
| PD-018 | Production logging config | — |
| PD-019 | Deploy documentation | Всі попередні |
| PD-020 | Smoke test script | Epic 2 deployed |

## Результат

- Push to main → automated deploy (test → build → deploy → smoke test)
- CORS restricted, security headers, no debug mode
- JSON structured logs в Docker
- README з повним описом deploy процесу
- Smoke test скрипт для post-deploy verification
