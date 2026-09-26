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
  - TEST_YAML_UNREADABLE
  - TEST_YAML_DUPLICATE_KEY
  - TEST_YAML_ALIAS
  - TEST_FIELD_INVALID
  - TEST_OPTIONS_COUNT
  - TEST_NO_CORRECT_OPTION
  - TEST_TOO_LARGE
  - SECURITY_REJECTED
  - NOT_A_TEST_OBJECT
  - GENERATION_IN_PROGRESS
  - TEST_FILE_NOT_YAML
  - TEST_OBJECT_SOURCE_RESERVED
  - TEST_OBJECT_TYPE_FIXED
  - TEST_IS_AN_OBJECT
  - TEST_OBJECT_NOT_PROCESSED
  - KEY_LIVES_IN_TEST
  - TASK_NOT_READY
  - TASK_LANGUAGE_UNSET
  - TASK_TEXT_TRUNCATED
  - NO_QUESTION_NUMBERS
  - KEY_DOES_NOT_MATCH_QUESTIONS
keywords:
  - помилки
  - коди помилок
  - тест
  - YAML
last_updated: 2026-09-26
---

# Коди помилок

Відмова API несе в полі `detail` код причини й пояснення: `{"code": "…", "details": "…"}`. Код —
сталий ключ, за яким ваша платформа добирає власні слова; `details` — запасний текст для коду,
якого вона ще не знає. Код із посиланням веде до розділу, де його описано докладно.

## Подача відповідей на тест

Коди, які отримує платформа школи, надсилаючи відповіді студента. Нічого з відмовленої подачі не
зберігається.

| код | стан | причина | що робити |
|---|---|---|---|
| [`TEST_ANSWERS_REQUIRED`](../api/index.md#TEST_ANSWERS_REQUIRED) | 422 | на тест надіслано файл | надіслати відповіді маршрутом подачі тесту |
| [`TEST_FORM_UNAVAILABLE`](../api/index.md#TEST_FORM_UNAVAILABLE) | 409 | тести тимчасово не приймаються відповідями | сказати студентові, що тест тимчасово недоступний, і спробувати пізніше |
| [`NOT_A_TEST_TASK`](../api/index.md#NOT_A_TEST_TASK) | 422 | **для тестів, створених у системі, не видається**: завдання, що не є опублікованим тестом, дістає 404 | якщо платформа вже обробляє код — перевірити, що вказано саме тестове завдання |
| [`TEST_VERSION_CHANGED`](../api/index.md#TEST_VERSION_CHANGED) | 409 | тест змінився, поки студент відповідав | прочитати тест знову й відповісти на нову версію |
| [`TEST_NOT_READY`](../api/index.md#TEST_NOT_READY) | 409 | **для тестів, створених у системі, не видається**: опублікована версія завжди має правильні відповіді | якщо платформа вже обробляє код — спробувати пізніше |
| [`ANSWERS_DO_NOT_MATCH_TEST`](../api/index.md#ANSWERS_DO_NOT_MATCH_TEST) | 422 | названо питання чи варіант, яких у тесті немає | звірити відповіді з читанням тесту й надіслати мітки так, як їх показав маршрут структури |

## Подія `failed` у вебхуку

Причина в полі `reason`, коли прийняту подачу не вдалося перевірити.

| причина | що сталося | що робити |
|---|---|---|
| [`path_failed`](../api/index.md#path_failed) | збій на нашому боці, рецензії не буде | запропонувати студентові надіслати відповіді ще раз |
| [`test_not_ready`](../api/index.md#test_not_ready) | **для тестів, створених у системі, не надходить** | якщо платформа вже обробляє причину — запропонувати спробувати пізніше |

## Тест, створений у системі

Коди, які отримує автор, створюючи, змінюючи й публікуючи тест. Нічого з відмовленого запиту не
зберігається. Відмови формату несуть ще й `place` — місце помилки (див.
[Коли тест відмовлено](../authors/index.md#refusals)).

| код | стан | причина | що робити |
|---|---|---|---|
| [`TEST_YAML_UNREADABLE`](../authors/index.md#TEST_YAML_UNREADABLE) | 422 | YAML чи JSON не читається | виправити запис у місці, яке називає `place` |
| [`TEST_YAML_DUPLICATE_KEY`](../authors/index.md#TEST_YAML_DUPLICATE_KEY) | 422 | те саме поле двічі в одному місці | лишити одне |
| [`TEST_YAML_ALIAS`](../authors/index.md#TEST_YAML_ALIAS) | 422 | якір чи посилання YAML (`&`, `*`) | записати текст повністю |
| [`TEST_FIELD_INVALID`](../authors/index.md#TEST_FIELD_INVALID) | 422 | поле невідоме, відсутнє, порожнє, не того виду чи задовге | виправити поле за `details` і таблицею формату |
| [`TEST_OPTIONS_COUNT`](../authors/index.md#TEST_OPTIONS_COUNT) | 422 | у питанні менше 2 чи більше 26 варіантів | змінити кількість варіантів |
| [`TEST_NO_CORRECT_OPTION`](../authors/index.md#TEST_NO_CORRECT_OPTION) | 422 | у питанні жодного правильного варіанта | позначити хоча б один `correct: true` |
| [`TEST_TOO_LARGE`](../authors/index.md#TEST_TOO_LARGE) | 413 | файл чи запит понад 256 КБ | розділити тест на кілька |
| [`SECURITY_REJECTED`](../authors/index.md#SECURITY_REJECTED) | 400 | невидимі символи, фрази, схожі на вказівки, чи файл, який не вдалося прочитати як текст | набрати текст заново чи зберегти файл в UTF-8 |
| [`NOT_A_TEST_OBJECT`](../authors/index.md#NOT_A_TEST_OBJECT) | 422 | ідентифікатор веде не до тесту, створеного в системі | перевірити ідентифікатор; старий текстовий тест створити заново з YAML |
| [`GENERATION_IN_PROGRESS`](../authors/index.md#GENERATION_IN_PROGRESS) | 409 | система саме пише пояснення до тесту | дочекатися, доки вона закінчить, і опублікувати ще раз |
| [`TEST_FILE_NOT_YAML`](../authors/index.md#TEST_FILE_NOT_YAML) | 422 | матеріал із видом «Тест» — не YAML-файл | записати тест у YAML і завантажити файлом |
| [`TEST_OBJECT_SOURCE_RESERVED`](../authors/index.md#TEST_OBJECT_SOURCE_RESERVED) | 422 | запит сам вказав вид джерела `test_object` | не вказувати його: завантажити YAML-файл або створити тест запитом |
| [`TEST_OBJECT_TYPE_FIXED`](../authors/index.md#TEST_OBJECT_TYPE_FIXED) | 422 | спроба змінити вид завдання тесту | створити новий матеріал |
| [`TEST_IS_AN_OBJECT`](../authors/index.md#TEST_IS_AN_OBJECT) | 422 | спроба зробити тестом наявний матеріал | створити тест із YAML |
| [`TEST_OBJECT_NOT_PROCESSED`](../authors/index.md#TEST_OBJECT_NOT_PROCESSED) | 422 | повторна обробка чи ролі файлів для тесту | змінити чернетку й опублікувати |
| [`KEY_LIVES_IN_TEST`](../authors/index.md#KEY_LIVES_IN_TEST) | 422 | заміна чи скидання ключа відповідей для тесту | змінити позначки в чернетці й опублікувати |

## Запити ключа відповідей

Для тесту, створеного в системі, запити ключа відповідей (`…/reference/override`) відмовляють кодом
[`KEY_LIVES_IN_TEST`](../authors/index.md#KEY_LIVES_IN_TEST). Коди нижче видають ці запити для
тесту, завантаженого текстом до появи тестів, створених у системі, і для матеріалу, що не є тестом.
Тест, завантажений текстом, відповідей більше не приймає — створіть його заново з YAML (див.
[Як створити тест](../authors/index.md#create)). Нічого з відмовленого запиту не зберігається.

| код | стан | причина | що робити |
|---|---|---|---|
| `NOT_A_TEST_TASK` | 422 | завдання — не тест; **для тестів, створених у системі, не видається** | перевірити, що завдання створене як тест |
| `TASK_NOT_READY` | 422 | завдання ще обробляється | дочекатися кінця обробки й надіслати ключ ще раз |
| `TASK_LANGUAGE_UNSET` | 422 | у матеріалу не вказано мову | вказати мову матеріалу й надіслати ключ ще раз |
| `TASK_TEXT_TRUNCATED` | 422 | текст тесту задовгий і обрізаний | скоротити тест або розділити на кілька |
| `NO_QUESTION_NUMBERS` | 422 | у тексті немає жодного номера питання | набрати номери `1.` на початку рядків власноруч |
| `KEY_DOES_NOT_MATCH_QUESTIONS` | 422 | номери в ключі не збігаються з тестом | виправити ключ за `details` і надіслати цілком |
| `GENERATION_IN_PROGRESS` | 409 | система ще пише пояснення або обробляє завдання | дочекатися, доки вона закінчить, і надіслати ключ ще раз |
