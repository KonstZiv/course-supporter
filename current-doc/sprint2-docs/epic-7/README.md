# EPIC-7: Integration Documentation + Manual QA

**Ціль:** Користувач API (зовнішній розробник) може самостійно пройти повний flow від створення курсу до отримання структури, використовуючи тільки документацію. Паралельно — manual QA всіх endpoints на production.

---

## Підхід

Три шари документації, кожен наступний глибший за попередній:

1. **Flow Guide** (Layer 1) — високорівневий опис: навіщо інструмент, які кроки, як вони пов'язані
2. **Quick Start** (Layer 2) — один конкретний найпростіший шлях, крок за кроком з curl-прикладами. Кожен крок тестується на production
3. **Endpoint Reference** (Layer 3) — детальний опис кожного endpoint з варіаціями параметрів, edge cases, помилками

**Метод роботи** — ітеративний з реальним тестуванням:
- Пишемо документацію для кроку → тестуємо на `api.pythoncourse.me` → знаходимо баги → фіксимо код → деплоїмо → оновлюємо документацію → наступний крок

---

## Задачі

- [S2-057: Flow Guide](./S2-057/README.md) — Layer 1
- [S2-058: Quick Start](./S2-058/README.md) — Layer 2 + manual QA happy path
- [S2-059: Endpoint Reference](./S2-059/README.md) — Layer 3 + manual QA варіацій

---

## API Surface (29 endpoints)

| Група | Кількість | Endpoints |
|-------|-----------|-----------|
| Health | 1 | GET /health |
| Courses | 3 | POST, GET list, GET detail |
| Nodes | 8 | POST root, POST child, GET tree, GET single, PATCH, POST move, POST reorder, DELETE |
| Materials (legacy) | 1 | POST upload |
| Material Entries | 5 | POST add, GET list, GET single, DELETE, POST retry |
| Slide-Video Mappings | 3 | POST batch, GET list, DELETE |
| Generation | 4 | POST trigger, GET latest, GET history, GET snapshot |
| Jobs | 1 | GET status |
| Reports | 1 | GET cost |

---

## Автоматизований контроль

- `mkdocs build --strict` — перевірка broken links
- OpenAPI schema validation — приклади відповідають реальній схемі

## Ручний контроль

Вбудований у процес: кожен curl-приклад у документації протестований на production.

---

## Обов'язкові дії після завершення Epic

1. **Оновити docs site** з новими сторінками
2. **Оновити Sprint 2 progress** — Epic 7 COMPLETE
3. **Зафіксувати знайдені та виправлені баги** у release notes
4. **PR review checklist:**
   - [ ] Всі curl-приклади працюють copy-paste
   - [ ] Всі error codes покриті
   - [ ] `mkdocs build --strict` проходить
   - [ ] Баги, знайдені під час QA, виправлені і мають unit tests
