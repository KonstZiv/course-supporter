---
title: Tests and criteria via the API
language: en
role: api
keywords:
  - api
  - authors
  - test
  - YAML
  - draft
  - publishing
  - explanations
  - criteria
  - text assignment
  - refusal codes
last_updated: 2026-11-24
---

# Tests and criteria via the API

This page is for school platforms that prepare a course with API requests, without the author app. If
you work in the author app, you need the [Tests](tests.md) page.

## How to create a test {#create}

**As a file.** In the author app, upload a YAML file (`.yaml` or `.yml`) into the right section of
the course, like any material, and choose the "Test (Тест)" assignment type. The system does not
process such a file like a lecture: it checks it straight away and creates a test with a draft; the
file itself is not stored. If the file has a format error, the test is not created, and the refusal
names its place (see [When a test is refused](#refusals)). An unfinished test is created — you can
finish it later (see [An unfinished draft](#incomplete)).

In the author app, a test is started with the "New test (Новий тест)" button and edited in the
[test editor](tests.md#editor). The API requests below are a reference for platforms that work with
tests without the author app: your school's access key with the course preparation right (scope
`PREP`) goes in the `X-API-Key` header, `$NODE_ID` is a section of the course, `$TEST_ID` is the test.

The same upload as a request — the source type (`source_type`) is required, but it does not matter
for a test:

```bash
curl -X POST https://api.pythoncourse.me/api/v1/nodes/$NODE_ID/documents \
     -H "X-API-Key: $PREP_KEY" \
     -F source_type=text -F task_type=test -F file=@lecture-3-test.yaml
```

**As a request.** The body is YAML (`Content-Type: application/yaml`) or the same structure in JSON
(`application/json`); the title (`title`) is required here:

```bash
curl -X POST https://api.pythoncourse.me/api/v1/nodes/$NODE_ID/tests \
     -H "X-API-Key: $PREP_KEY" -H "Content-Type: application/yaml" \
     --data-binary @lecture-3-test.yaml
```

Response **201** — the test: `id` (this is the `$TEST_ID`), `title`, `draft` — questions with
numbers, options with letters and marks — and `published: null`, and next to them the fields
described in [What reading the draft shows](#draft). An upload as a file also returns the test's `id`.

## Draft and publishing {#publish}

Read the draft — with option letters and marks — and the current version:

```bash
curl https://api.pythoncourse.me/api/v1/tests/$TEST_ID/draft -H "X-API-Key: $PREP_KEY"
```

`published` is the current version: `number` (its sequence number), `version` (a tag of what the
student sees) and `published_at`; before the first publication — `null`. The other fields — what is
left to finish, what the check found, whether there are unpublished changes — are described in
[What reading the draft shows](#draft).

Replace the draft — as a whole, YAML or JSON, as when creating:

```bash
curl -X PUT https://api.pythoncourse.me/api/v1/tests/$TEST_ID/draft \
     -H "X-API-Key: $PREP_KEY" -H "Content-Type: application/yaml" \
     --data-binary @lecture-3-test.yaml
```

A draft with a format error is not saved — the previous one stays; an unfinished one is saved as it
is (see [An unfinished draft](#incomplete)). A `title` in the new draft renames the test straight
away, without publishing; without a `title`, the title stays the same. Students do not see the other
edits until you publish the draft. You can check it before publishing (see [Check the draft](#check)).

```bash
curl -X POST https://api.pythoncourse.me/api/v1/tests/$TEST_ID/publish -H "X-API-Key: $PREP_KEY"
```

Response **201** — a new version is published; **200** — the draft is the same as the current
version, and nothing has changed. The new version immediately becomes the one students see and take.
Explanations in the course language for new questions, options or marks are written by the system
during the draft check, or, if the draft was not checked, after publishing; publishing a checked draft
takes them ready-made and charges nothing extra (see [Explanations](#explanations)).

**422** [`TEST_DRAFT_INCOMPLETE`](#TEST_DRAFT_INCOMPLETE) — the draft is not finished; nothing is
published.

**409** [`GENERATION_IN_PROGRESS`](#GENERATION_IN_PROGRESS) — the system is writing explanations for
this test right now; nothing from the publication is saved. Wait until it finishes and publish again.

### An unfinished draft {#incomplete}

You can write a test in parts: the draft is saved even without questions, with a question without
options or with one, without a `correct: true` mark, with an empty question or option text. Only a
finished draft can be checked and published — an unfinished one is refused by both actions with the
code [`TEST_DRAFT_INCOMPLETE`](#TEST_DRAFT_INCOMPLETE), and nothing changes.

What is left to finish, reading the draft shows in the `incomplete` field — in the order you read the
test: question text, option texts, how many options, whether a correct one is marked. Each place is a
code, a question number (`question`) and an option number (`option`), counting from 1; whatever the
place does not concern is `null`. An empty list means the draft is finished.

```json
"incomplete": [
  {"code": "TEST_TEXT_EMPTY", "question": 1, "option": 2},
  {"code": "TEST_NO_CORRECT_OPTION", "question": 3, "option": null}
]
```

| code | place | what is wrong | what to do |
|---|---|---|---|
| `TEST_NO_QUESTIONS` {#TEST_NO_QUESTIONS} | test | there are no questions | add questions |
| `TEST_TEXT_EMPTY` {#TEST_TEXT_EMPTY} | question, or, when `option` is given, option | empty text | write the text |
| [`TEST_OPTIONS_COUNT`](#TEST_OPTIONS_COUNT) | question | fewer than two options | add options |
| `TEST_NO_CORRECT_OPTION` {#TEST_NO_CORRECT_OPTION} | question with options | no option is marked `correct: true` | mark the correct options |

### Check the draft {#check}

The check shows, before publishing, what the system will write for the test: an explanation for each
question in the course language and doubts about your marks (see [Doubt](tests.md#doubt)). The check
publishes nothing: students see nothing of it.

```bash
curl -X POST https://api.pythoncourse.me/api/v1/tests/$TEST_ID/check -H "X-API-Key: $PREP_KEY"
```

Response **200** — the state of the check: `state`, `explanations` and `doubts`, as in the `check`
field of reading the draft (see [What reading the draft shows](#draft)). `in_progress` — the system is
writing explanations; when it is done, reading the draft will show `ready`. `ready` — explanations for
these same questions, options and marks are already written, and they are already in the response.

What it costs:

- **A draft that has no explanations yet** — new, or changed in questions, options or marks — one
  writing of explanations: as much as publishing it without a check would cost.
- **An unchanged draft that is already checked or published** — nothing: the system returns what it
  has already written. A repeated request while the check is running costs nothing either.
- **Publishing a checked draft** charges nothing extra, even while the check is still running: the new
  version takes the explanations the check wrote or is writing.
- **The pass mark and your own explanations** do not reset the check: if you changed only them, there
  is no need to check again, and publishing will not charge anything extra either.

If the check failed (`failed` in reading the draft), check again — the system will try anew. You can
also publish without a check: then the system writes the explanations after publishing.

**422** [`TEST_DRAFT_INCOMPLETE`](#TEST_DRAFT_INCOMPLETE) — the draft is not finished; nothing is
checked. **409** [`GENERATION_IN_PROGRESS`](#GENERATION_IN_PROGRESS) — the system is writing other
explanations for this test right now; nothing is saved — check again when it finishes.

### What reading the draft shows {#draft}

Reading the draft — like the response to creating and replacing — carries, besides the draft itself
(`draft`) and the current version (`published`):

- **`incomplete`** — what is left to finish (see [An unfinished draft](#incomplete)); an empty list
  means the draft is finished.
- **`check`** — what the check found for the draft as it is: `state` — the state of the check (table
  below), `explanations` — the system's explanations by question number, `doubts` — questions whose
  marks the system has doubts about, for example `{"2": true}` (see [Doubt](tests.md#doubt)). Until
  the state is `ready`, `explanations` and `doubts` are empty. Your own explanations are in the draft
  itself, separate from the system's explanations.
- **`unpublished_changes`** — whether publishing will create a new version: `true` when the draft
  differs from the current version in questions, options, marks, pass mark, your explanations or the
  course language, and before the first publication. A new title does not add changes: it is visible
  straight away.
- **`course_root_id`** — the course the test is in.

| `check.state` | what it means |
|---|---|
| `not_checked` | the draft was not checked, was changed in questions, options or marks after the check, or is not finished yet |
| `in_progress` | the system is writing explanations |
| `ready` | explanations and doubts are written |
| `failed` | the explanations could not be written — check again |

For example, this is what these fields look like for a draft that is checked but not yet published:

```json
{
  "incomplete": [],
  "check": {"state": "ready", "explanations": {"1": "…", "2": "…"}, "doubts": {"2": true}},
  "unpublished_changes": true,
  "course_root_id": "…"
}
```

Reading orders nothing and costs nothing.

## Explanations {#explanations}

The system writes an explanation for each question in the course language — once for the same
questions, options and marks: when you check the draft (see [Check the draft](#check)), or, if you did
not check it, after a publication that changed questions, options or marks. The state of the
explanations of the current version:

```bash
curl https://api.pythoncourse.me/api/v1/documents/$TEST_ID/reference -H "X-API-Key: $PREP_KEY"
```

The response carries `status`: `awaiting_key` — the test is not published yet, `generating` — the
explanations are being written, `ready` — everything is ready, `failed` — the explanations could not
be written (the cause is named in `failure_reason`; to try again, publish the test once more). If the
explanations were not written because your package ran out of units, `failure_code` is
`insufficient_units`: buy a package in the author app and publish the test again; for other causes, `failure_code` is `null`. Next to it — `answers` (the letters
of the correct options of the current version), `pass_threshold`, `explanations` (your own and those
the system wrote) and `doubts` (see [Doubt](tests.md#doubt)). What the check found for a draft that is
not yet published, this request does not show — reading the draft shows that (`check`, see
[What reading the draft shows](#draft)).

## Download a test as YAML {#export}

```bash
curl https://api.pythoncourse.me/api/v1/tests/$TEST_ID/yaml -H "X-API-Key: $PREP_KEY" -o lecture-3-test.yaml
```

The response is the draft together with the title in YAML format, with the fields in the same order as
in the example on the [Tests](tests.md#yaml) page. You can correct this file and upload it back by
replacing the draft. The download contains the correct answers, so it is given only to your school's
key with the course preparation right; the student and the school's platform do not get it.

## Hide a test {#hide}

Hide a test with the "Hide test (Приховати тест)" button — in the test editor or in the test's row in
the course tree — or with the request `DELETE /api/v1/documents/$TEST_ID`. The test disappears from
the course for students. The system keeps attempts already made and their reviews, but the student
can no longer reach them through the course.

The role of a test cannot be changed: a test is always an educational material. Students do not see
methodological material, so the test would quietly disappear from the course; such a request is
refused with the code [`TEST_OBJECT_ROLE_FIXED`](#TEST_OBJECT_ROLE_FIXED).

## When a test is refused {#refusals}

A refusal carries the reason code and an explanation in the `detail` field, and a format refusal also
the place: `{"code": "…", "details": "…", "place": {…}}`. `place` names the line and column in the
YAML (`line`, `column`) and the question and option number (`question`, `option`), counting from 1;
whatever is not there is `null`. A refusal of an unfinished draft carries the `incomplete` list
instead of the place (see [`TEST_DRAFT_INCOMPLETE`](#TEST_DRAFT_INCOMPLETE)). Nothing from a refused
request is saved: no test, no change to the draft, no new version.

### `TEST_YAML_UNREADABLE` {#TEST_YAML_UNREADABLE}

**422.** The YAML (or JSON) cannot be read — a syntax error, for example uneven indentation or a colon
in a text without quotes. `place` names the line and column. Fix the entry at this place.

### `TEST_YAML_DUPLICATE_KEY` {#TEST_YAML_DUPLICATE_KEY}

**422.** The same field twice in one place — for example, two `text` in one option. `place` names the
second one. Keep one.

### `TEST_YAML_ALIAS` {#TEST_YAML_ALIAS}

**422.** The file has a YAML anchor or alias — `&` or `*` before a name. Write the text out in full in
every place where it is needed.

### `TEST_FIELD_INVALID` {#TEST_FIELD_INVALID}

**422.** A field is unknown or missing, the title is empty, a field is of the wrong kind (for example,
`correct: так` instead of `true`, or a pass mark of `80.5`) or is too long. `details` says what
exactly is wrong, `place` — where. Fix the field according to the [format table](tests.md#yaml).
Creating a test with a request without `title` also gives this refusal.

### `TEST_OPTIONS_COUNT` {#TEST_OPTIONS_COUNT}

**422.** A question has more than 26 options. `place` names the question. Split the question into
several.

The same code in the `incomplete` list means something else: the question has fewer than two options.
Such a draft is saved, but it can be checked and published only after adding options (see
[An unfinished draft](#incomplete)).

### `TEST_TOO_LARGE` {#TEST_TOO_LARGE}

**413.** The file or request body is larger than 256 KB. Split the test into several.

### `SECURITY_REJECTED` {#SECURITY_REJECTED}

**400.** The test text contains invisible characters (this happens with text copied from Word or a web
page) or phrases resembling instructions to the system that writes explanations, or the file could not
be read as text. The `category` field names what exactly was found: `suspicious_unicode`,
`prompt_injection` or `charset_violation`. Type the suspicious text again — or save the file in UTF-8
encoding — and send the test once more.

### `NOT_A_TEST_OBJECT` {#NOT_A_TEST_OBJECT}

**422.** The identifier leads to another material of your school, not to a test created in the system:
for example, to a lecture or to a test once uploaded as text. Check the identifier. A test uploaded as
text no longer accepts answers — create it again from YAML.

**404** `Document not found` — the test does not exist, has been deleted or belongs to another school;
**404** `Node not found` — the same for the section in which you are creating the test. **415** — the
request body is neither YAML nor JSON.

### `TEST_DRAFT_INCOMPLETE` {#TEST_DRAFT_INCOMPLETE}

**422.** Checking or publishing an unfinished draft. Instead of `place`, the refusal carries the
`incomplete` list — the same as reading the draft: each place left to finish, with a code and the
question and option numbers (see [An unfinished draft](#incomplete)):

```json
{"detail": {"code": "TEST_DRAFT_INCOMPLETE", "details": "…",
            "incomplete": [{"code": "TEST_NO_CORRECT_OPTION", "question": 2, "option": null}]}}
```

Nothing is checked or published. Finish every place from the list, save the draft and repeat the check
or the publication.

### `GENERATION_IN_PROGRESS` {#GENERATION_IN_PROGRESS}

**409.** The system is writing explanations for this test right now — for another state of the draft,
for the current version or in the language of one of the students. Nothing from the check or
publication is saved. Wait until it finishes and repeat the request.

## Refusals when uploading and changing a material {#material-refusals}

These codes are given by the material routes — upload, change, repeated processing — when the matter
concerns a test. The body is `{"code": "…", "details": "…"}`; nothing is saved.

### `TEST_FILE_NOT_YAML` {#TEST_FILE_NOT_YAML}

**422.** A material with the "Test" assignment type is not a YAML file (`.yaml`, `.yml`) but another
file or a link. The system no longer accepts a test from text or a Word document. Write the test in
YAML and upload it as a file.

### `TEST_OBJECT_SOURCE_RESERVED` {#TEST_OBJECT_SOURCE_RESERVED}

**422.** The request itself specified the source type `test_object` — only the system sets this type.
Upload a YAML file with the "Test" assignment type or create the test with a request (see
[How to create a test](#create)).

### `TEST_OBJECT_TYPE_FIXED` {#TEST_OBJECT_TYPE_FIXED}

**422.** An attempt to change the assignment type of a test created in the system. The test stays a
test; if you need another assignment, create a new material.

### `TEST_OBJECT_ROLE_FIXED` {#TEST_OBJECT_ROLE_FIXED}

**422.** An attempt to make a test created in the system a methodological material — to give it a role
(`material_role`) other than `educational`. A test is always educational: students do not see
methodological material. The role stays educational; to remove the test from students, hide it (see
[Hide a test](#hide)).

### `TEST_IS_AN_OBJECT` {#TEST_IS_AN_OBJECT}

**422.** An attempt to turn an existing material into a test by changing its assignment type. A test is
created from YAML — upload a file or create the test with a request.

### `TEST_OBJECT_NOT_PROCESSED` {#TEST_OBJECT_NOT_PROCESSED}

**422.** Repeated processing or file roles for a test created in the system: such a test is not
processed like a lecture. To change the test, change the draft and publish it.

### `KEY_LIVES_IN_TEST` {#KEY_LIVES_IN_TEST}

**422.** A request to replace or remove the answer key (`PUT` or `DELETE …/reference/override`) for a
test created in the system. The correct answers are the `correct` marks in the test itself: change the
draft and publish it.

## Criteria of a text assignment {#criteria}

The system reviews students' work on a text assignment — a short assignment, an assignment or a
project — against a list of criteria. The system puts the list together itself from the assignment's
text, title and description, the section description and the core concepts of the section and the
course. It does so once — after the first piece of work a student submits for review — and from then
on every piece of work on this assignment is reviewed against the same list. You can correct it.

The section description in the author app — the "Node description (Опис вузла)" window — gives the
list the description of the assignment's section, and the core concepts (the "Core concepts (Основні
концепти)" field) — those of the assignment's section and the course. If the list was put together
when neither the section nor the course had a description, the system will put it together once more
— after the first submission that arrives once a description has appeared — and only once.

Each criterion carries:

| field | what it is |
|---|---|
| `id` | the criterion's tag — `c1`, `c2`, …; set by the system |
| `text` | what to check |
| `evidence` | the sign by which you can see it is done |
| `weight` | weight: `must` — major, `should` — moderate, `may` — minor |
| `check_method` | how to check: `model_verdict` — the system assesses the work by the criterion's text and sign; `mandatory_points` — the criterion is met when the work contains every mandatory point; `code_test` — a check by running code, only in a project |
| `soft_descent` | `true` — the check by running code is done by the system's assessment for now: the system cannot run code yet |
| `concepts` | the core concepts the criterion concerns |
| `mandatory_points` | mandatory points — for `mandatory_points`; each has its own tag: `c1.p1`, `c1.p2`, … |
| `must_by_author` | only in your edit: `true` — you made this `must` mandatory for passing yourself; `false` — no (a `must` mark suggested by the automatic check affects only the score). This field is not present in the system's list |

### How passing is decided {#criteria-pass}

The work's score is the share of the weights of the criteria met (`must` — 3, `should` — 2, `may` —
1), as a percentage, rounded down. Only a criterion you marked as mandatory yourself
(`must_by_author: true`) is mandatory for passing. Whether the work is passed:

- **your list** (you saved an edit, see [Correct the list](#criteria-edit)) and it has at least one
  `must` that you marked as mandatory yourself — the work is passed if every such `must` is met and
  the score is at least 70;
- **the system's list**, or your list without a single such `must` — the work is passed if the score
  is at least 80.

`must` marks suggested by the automatic check affect only the score until you confirm them: such a
criterion is assessed, shown in the review and counts in the score, but does not block passing by
itself — neither in the system's list nor in your list if you saved it without changing anything in the
mark. For a criterion to become mandatory for passing, send `"must_by_author": true` in the edit
(see [Correct the list](#criteria-edit)). The review explains to the student by which rule it was
decided: it names the score and the pass mark.

### Read the list {#criteria-read}

```bash
curl https://api.pythoncourse.me/api/v1/documents/$TASK_ID/criteria -H "X-API-Key: $PREP_KEY"
```

The response carries `status` — whether there is a list, and if not, why:

- `ready` — there is a list;
- `awaiting_first_submission` — there is no list yet, and no student work is waiting for it;
- `composing` — the system is putting the list together right now; this takes a few minutes;
- `not_composed` — there is no list, although students' work is waiting for it, or the last attempt to
  put it together failed.

While there is no list, `message` says what happened and what to do, `reason_code` in the
`not_composed` state says why there is no list, and `waiting_submissions` says how many pieces of
students' work are waiting for the list in order to be reviewed against it (see
[When there is no list](#criteria-none)). Next to them:

- `model` — the list the system put together;
- `author` — your edit, if you made one; each of its criteria carries `must_by_author`;
- `in_force` — the list work is reviewed against: `layer` says whose it is (`author` — your edit,
  `model` — the system's list), `criteria` — the criteria themselves;
- `contradictions` — where the assignment contradicts the section description. This is only for you:
  it goes neither into the criteria nor into the score;
- `concepts` — the concepts that can be named in criteria.

#### When there is no list {#criteria-none}

The server returns the `message` texts in Ukrainian; each is followed here by an English translation.

While no work is waiting for the list (`awaiting_first_submission`), the `message` is:

> Перелік критеріїв буде складено після першої подачі роботи студентом на перевірку, після цього його можна поправити. Ви можете скласти його й зараз, не чекаючи першої подачі.

*In English: The list of criteria will be put together after a student first submits work for review; after that, it can be corrected. You can put it together now, without waiting for the first submission.*

While the system is putting it together (`composing`):

> Перелік критеріїв складається зараз. Роботи студентів, що чекають на нього, буде перевірено, щойно він буде готовий; після цього перелік можна поправити.

*In English: The list of criteria is being put together now. Students' work waiting for it will be reviewed as soon as it is ready; after that, the list can be corrected.*

If there is no list for another reason (`not_composed`), `reason_code` names it, and `message` says
what to do:

| `reason_code` | `message` | in English |
|---|---|---|
| `task_not_processed` | Перелік критеріїв ще не складено: обробку тексту завдання не завершено. Якщо обробка ще триває — дочекайтеся її завершення; якщо вона завершилася з помилкою — запустіть її знову. Після цього перелік складеться автоматично, щойно хтось зі студентів подасть роботу на це завдання, і тоді ж буде перевірено роботи, що чекають. Якщо нових робіт найближчим часом не буде — зверніться до адміністратора системи, і він запустить повторну спробу. | The list of criteria has not been put together yet: processing of the assignment text has not finished. If processing is still running, wait for it to finish; if it ended with an error, run it again. After that, the list will be put together automatically as soon as one of the students submits work for this assignment, and the waiting work will be reviewed at the same time. If no new work is expected soon, contact the system administrator, and they will start another attempt. |
| `models_unavailable` | Перелік критеріїв не вдалося скласти: сервіс, що його складає, був тимчасово недоступний. Від вас нічого не потрібно: повторна спроба відбудеться автоматично, щойно хтось зі студентів подасть роботу на це завдання, і тоді ж буде перевірено роботи, що чекають. Якщо нових робіт найближчим часом не буде або стан не зміниться — зверніться до адміністратора системи. | The list of criteria could not be put together: the service that puts it together was temporarily unavailable. Nothing is needed from you: another attempt will happen automatically as soon as one of the students submits work for this assignment, and the waiting work will be reviewed at the same time. If no new work is expected soon or the state does not change, contact the system administrator. |
| `limit_reached` | Перелік критеріїв не вдалося скласти: завдання завелике для обмежень, що зараз діють у системі. Зверніться до адміністратора системи: після зміни обмежень він запустить повторну спробу, і тоді ж буде перевірено роботи, що чекають. Не правте заради цього текст завдання: правка створить нову версію завдання й не допоможе роботам, що вже чекають. | The list of criteria could not be put together: the assignment is too large for the limits currently in force in the system. Contact the system administrator: after the limits are changed, they will start another attempt, and the waiting work will be reviewed at the same time. Do not edit the assignment text for this: an edit creates a new version of the assignment and will not help the work that is already waiting. |
| `composition_error` | Перелік критеріїв не вдалося скласти через внутрішню помилку системи. Зверніться до адміністратора системи: після виправлення він запустить повторну спробу, і тоді ж буде перевірено роботи, що чекають. | The list of criteria could not be put together because of an internal system error. Contact the system administrator: after the fix, they will start another attempt, and the waiting work will be reviewed at the same time. |
| `not_attempted` | Перелік критеріїв для поточної версії завдання ще не складався, а роботи студентів уже чекають на нього. Він складеться автоматично, щойно хтось зі студентів подасть роботу на це завдання, і тоді ж буде перевірено роботи, що чекають. Якщо нових робіт найближчим часом не буде — зверніться до адміністратора системи, і він запустить повторну спробу. | The list of criteria for the current version of the assignment has not been put together yet, and students' work is already waiting for it. It will be put together automatically as soon as one of the students submits work for this assignment, and the waiting work will be reviewed at the same time. If no new work is expected soon, contact the system administrator, and they will start another attempt. |

Student work for which there is no list waits until the list appears and gets its review at that point; `waiting_submissions` shows how many such pieces of work there are.

### Correct the list {#criteria-edit}

An edit is sent as the whole list:

```bash
curl -X PUT https://api.pythoncourse.me/api/v1/documents/$TASK_ID/criteria/override \
     -H "X-API-Key: $PREP_KEY" -H 'Content-Type: application/json' \
     -d '{"criteria": [{"id": "c1", "text": "Обробляє n = 0", "evidence": "factorial(0) повертає 1", "weight": "must", "check_method": "model_verdict", "concepts": ["Рекурсія"], "must_by_author": true}, {"text": "Є базовий випадок", "evidence": "рекурсія зупиняється", "weight": "should", "check_method": "mandatory_points", "mandatory_points": [{"text": "умова зупинки"}]}]}'
```

- `must_by_author` — whether a `must` criterion is mandatory for passing (see
  [How passing is decided](#criteria-pass)):
    - `true` — mandatory; `false` — no, the criterion only counts in the score;
    - not sent — a criterion that was `must` in your previous edit keeps its value from there: your
      choice of `true` or `false` does not change until you send something else; a criterion whose
      weight you raised to `must` in this edit and a new `must` criterion become mandatory; a `must`
      that you kept from the system's list and have not marked yet does not become mandatory until
      you send `true`;
    - only for `must`: `true` in a criterion with another weight is a
      [`MUST_BY_AUTHOR_WITHOUT_MUST`](#MUST_BY_AUTHOR_WITHOUT_MUST) refusal.
- The response shows `must_by_author` in every criterion of your edit. An edit saved before this
  field appeared does not have it: none of its `must` criteria is mandatory for passing until you
  save an edit with `true`.

- Send a criterion or point you are keeping with its `id` — the tag will be kept. A new one — without
  `id`: the system gives it a tag after the largest one in the current list and in the system's list,
  and a new point — after the largest among the points of its criterion. A criterion that is not in
  the edit is removed from the list.
- `soft_descent` is not sent — the system sets it.
- Concepts — only from the `concepts` of reading.
- Limits: up to 60 criteria; text and evidence — up to 600 characters; up to 10 mandatory points per
  criterion, a point — up to 300 characters; up to 10 concepts per criterion. Breaking a limit gives a
  **422** refusal that names the field.
- An edit does not change the contradictions.

The edit applies to the assignment in the form it had when you made the edit. If the assignment's text
or kind changes, the system puts together a new list after the next submission, and the previous edit
stops applying and disappears from reading — correct the new list.

### Reset the edit {#criteria-reset}

```bash
curl -X DELETE https://api.pythoncourse.me/api/v1/documents/$TASK_ID/criteria/override -H "X-API-Key: $PREP_KEY"
```

Work is again reviewed against the system's list. A reset edit, like every replaced one, stays in the
system as a snapshot, but does not apply and is not shown in reading.

All three requests respond with the same body as reading — so you can see what exactly applies now.

### When a request is refused {#criteria-refusals}

A refusal carries the reason code and an explanation in the `detail` field:
`{"code": "…", "details": "…"}`. Nothing from a refused request is saved.

| code | status | reason | what to do |
|---|---|---|---|
| `NOT_A_TEXT_TASK` {#NOT_A_TEXT_TASK} | 422 | the material is a test or not an assignment | a test is checked against your marks of the correct answers (see [The test editor](tests.md#editor)); a material that is not an assignment has no criteria |
| `TASK_NOT_READY` {#TASK_NOT_READY} | 422 | the assignment is still being processed or has been removed from the course | wait for processing to finish and repeat the request |
| `AWAITING_FIRST_SUBMISSION` {#AWAITING_FIRST_SUBMISSION} | 422 | an edit while there is no list | read the list: `status` and `message` say why there is none and what to do (see [When there is no list](#criteria-none)); edit when `status` is `ready` |
| `UNKNOWN_CRITERION_ID` {#UNKNOWN_CRITERION_ID} | 422 | the edit has a criterion or point `id` that is not in the current list | read the list again and send the edit with its `id`s; send a new criterion without `id` |
| `CHECK_METHOD_NOT_ADMITTED` {#CHECK_METHOD_NOT_ADMITTED} | 422 | the check method is not for this assignment type: `code_test` — only in a project | choose `model_verdict` or `mandatory_points` |
| `UNKNOWN_CONCEPT` {#UNKNOWN_CONCEPT} | 422 | a concept that is not in the `concepts` of reading | choose a concept from `concepts` or remove it |
| `MUST_BY_AUTHOR_WITHOUT_MUST` {#MUST_BY_AUTHOR_WITHOUT_MUST} | 422 | `must_by_author: true` in a criterion whose weight is not `must` | set the criterion's weight to `must` or remove `must_by_author` |
