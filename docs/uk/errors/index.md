---
title: Коди помилок
language: uk
role: reference
error_codes:
  - TEST_ANSWERS_REQUIRED
  - TEST_FORM_UNAVAILABLE
  - NOT_A_TEST_TASK
  - TEST_VERSION_CHANGED
  - TEST_NOT_READY
  - ANSWERS_DO_NOT_MATCH_TEST
  - path_failed
  - test_not_ready
  - TASK_NOT_READY
  - TASK_LANGUAGE_UNSET
  - TASK_TEXT_TRUNCATED
  - NO_QUESTION_NUMBERS
  - KEY_DOES_NOT_MATCH_QUESTIONS
  - GENERATION_IN_PROGRESS
keywords:
  - помилки
  - коди помилок
  - тест
  - ключ відповідей
last_updated: 2026-09-24
---

# Коди помилок

Відмова API несе в полі `detail` код причини й пояснення: `{"code": "…", "details": "…"}`. Код —
сталий ключ, за яким ваша платформа добирає власні слова; `details` — запасний текст для коду,
якого вона ще не знає. Кожен код нижче веде до розділу, де його описано докладно.

## Подача відповідей на тест

Коди, які отримує платформа школи, надсилаючи відповіді студента. Нічого з відмовленої подачі не
зберігається.

| код | стан | причина | що робити |
|---|---|---|---|
| [`TEST_ANSWERS_REQUIRED`](../api/index.md#TEST_ANSWERS_REQUIRED) | 422 | на тест надіслано файл | надіслати відповіді маршрутом подачі тесту |
| [`TEST_FORM_UNAVAILABLE`](../api/index.md#TEST_FORM_UNAVAILABLE) | 409 | тести поки що приймаються файлом | надіслати роботу файлом |
| [`NOT_A_TEST_TASK`](../api/index.md#NOT_A_TEST_TASK) | 422 | завдання — не тест | надіслати роботу файлом або вказати тестове завдання |
| [`TEST_VERSION_CHANGED`](../api/index.md#TEST_VERSION_CHANGED) | 409 | тест змінився, поки студент відповідав | прочитати тест знову й відповісти на нову версію |
| [`TEST_NOT_READY`](../api/index.md#TEST_NOT_READY) | 409 | для поточної версії тесту немає ключа відповідей | спробувати пізніше, коли читання тесту покаже `accepting_answers: true` |
| [`ANSWERS_DO_NOT_MATCH_TEST`](../api/index.md#ANSWERS_DO_NOT_MATCH_TEST) | 422 | названо питання чи варіант, яких у тесті немає | звірити відповіді з читанням тесту й надіслати знову |

## Подія `failed` у вебхуку

Причина в полі `reason`, коли прийняту подачу не вдалося перевірити.

| причина | що сталося | що робити |
|---|---|---|
| [`path_failed`](../api/index.md#path_failed) | збій на нашому боці, рецензії не буде | запропонувати студентові надіслати відповіді ще раз |
| [`test_not_ready`](../api/index.md#test_not_ready) | поки подача чекала перевірки, автор прибрав або змінив ключ | спробувати пізніше, коли тест знову приймає відповіді |

## Ключ відповідей тесту

Коди, які отримує автор, задаючи ключ відповідей. Нічого з відмовленого запиту не зберігається.

| код | стан | причина | що робити |
|---|---|---|---|
| [`NOT_A_TEST_TASK`](../authors/index.md#NOT_A_TEST_TASK) | 422 | завдання — не тест | перевірити, що завдання створене як тест |
| [`TASK_NOT_READY`](../authors/index.md#TASK_NOT_READY) | 422 | завдання ще обробляється | дочекатися кінця обробки й надіслати ключ ще раз |
| [`TASK_LANGUAGE_UNSET`](../authors/index.md#TASK_LANGUAGE_UNSET) | 422 | у матеріалу не вказано мову | вказати мову матеріалу й надіслати ключ ще раз |
| [`TASK_TEXT_TRUNCATED`](../authors/index.md#TASK_TEXT_TRUNCATED) | 422 | текст тесту задовгий і обрізаний | скоротити тест або розділити на кілька |
| [`NO_QUESTION_NUMBERS`](../authors/index.md#NO_QUESTION_NUMBERS) | 422 | у тексті немає жодного номера питання | набрати номери `1.` на початку рядків власноруч |
| [`KEY_DOES_NOT_MATCH_QUESTIONS`](../authors/index.md#KEY_DOES_NOT_MATCH_QUESTIONS) | 422 | номери в ключі не збігаються з тестом | виправити ключ за `details` і надіслати цілком |
| [`GENERATION_IN_PROGRESS`](../authors/index.md#GENERATION_IN_PROGRESS) | 409 | система ще пише пояснення або обробляє завдання | дочекатися, доки вона закінчить, і надіслати ключ ще раз |
