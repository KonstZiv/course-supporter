# PD-019: Deploy Documentation

## Що

README/docs з повним описом: як розгорнути з нуля, як робити повторний deploy, env vars reference, troubleshooting.

## Навіщо

Документація — частина Definition of Done спрінту. Нова людина (або ти сам через 3 місяці) повинна змогти розгорнути production за годину.

## Ключові рішення

- `docs/deployment.md` — основний файл
- Секції: Prerequisites, First Deploy, Subsequent Deploys, Env Vars, Troubleshooting
- Не дублювати README — посилатися на нього

## Acceptance Criteria

- [ ] `docs/deployment.md` створено
- [ ] First deploy instructions — від нуля до працюючого API
- [ ] Env vars reference з описом кожної змінної
- [ ] Troubleshooting: топ-5 типових проблем
- [ ] Rollback procedure
