# S2-000b: GitHub Actions → GitHub Pages deploy

**Epic:** EPIC-0 — Project Documentation Infrastructure
**Оцінка:** 2h

---

## Мета

Автоматичний deploy документації при push в main

## Що робимо

Налаштувати GitHub Actions workflow для mkdocs gh-deploy

## Як робимо

1. Створити .github/workflows/docs.yml
2. Workflow: checkout → setup python → install deps → mkdocs gh-deploy
3. Увімкнути GitHub Pages в Settings → Pages → Source: gh-pages branch
4. Перевірити що push в main тригерить deploy

## Очікуваний результат

Push в main → через 1-2 хв docs site оновлюється на GitHub Pages

## Як тестуємо

**Автоматизовано:** GitHub Actions workflow green

**Human control:** Зробити commit в main, через 2 хв відкрити GitHub Pages URL, перевірити що зміни з'явились

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
