---
title: Tests
language: en
role: authors
keywords:
  - authors
  - test
  - test editor
  - YAML
  - draft
  - draft check
  - publishing
  - pass mark
  - explanations
last_updated: 2026-11-22
---

# Tests

## A test checked by the system {#test}

A test is checked against the correct answers you marked in the test itself: the system compares the
student's answers with them. The student marks options right in the portal (or on your school's
platform) and gets a review straight away, without any person involved: a score, a verdict and an
explanation for each wrong answer.

The system writes the explanations itself — from the questions and your marks. You can write your
own explanation for any question, and the student will see exactly that.

A test lives in the system in two forms: a **draft**, which you edit, and **published versions**,
which students see. Until you publish the test for the first time, students do not see it at all.
You can check a draft before publishing: the system will write explanations for the questions and
say which of your marks it has doubts about, and students will see none of this (see
[Check with the model](#editor-check)).

A test is written, checked and published in the [test editor](#editor) of the author app.

## The test editor {#editor}

In the test editor, you write, check and publish a test right in the author app — without YAML and
without requests. A YAML file is still there for uploading a finished test or downloading one, and
the API requests described on the page [Tests and criteria via the API](api.md) are a reference for
platforms that work with tests without the author app.

![The test editor with two questions, the model's explanations and the model's doubt on the second question (Ukrainian interface)](/assets/test-editor-questions.png)

*The test editor. At the top — the title, the test's state and actions; below — the title and the
pass mark, then the question cards. The draft has been checked by the model: next to each question
is the model's explanation, and the model has doubts about the marks of the second question.*

### Open a test or start a new one {#editor-open}

- **An existing test.** In the course tree, choose the section and, in the test's row, press "Open
  test (Відкрити тест)". In the tree, a test is called by its title.
- **A new test.** Press "New test (Новий тест)" in the section menu (right-click the section card) or
  the button of the same name in the section panel. The editor opens empty: type the test title and
  press "Save draft (Зберегти чернетку)". The test appears in the section: "Test created. Students
  will not see it until you publish it." («Тест створено. Студенти не побачать його, доки ви його не
  опублікуєте.»)
- The arrow to the left of the title ("Back to course (До курсу)") takes you back to the course.

### Title, pass mark and questions {#editor-questions}

At the top are "Test title (Назва тесту)" and "Pass mark, % (Поріг заліку, %)". Students see the
test under its title; a new title takes effect right after saving, without publishing. The pass mark
is optional: with it, the student sees "passed" or "not passed", without it — only the score (see
[How the score is calculated](#score)).

Below is a card for each question: "Question text (Текст питання)" and "Answer options (Варіанти
відповіді)", and next to each option a "correct (правильна)" checkbox. Mark all correct options:
there can be several, and then say in the question text how many answers to choose. "Add question
(Додати питання)" and "Add option (Додати варіант)" add new ones, the up and down arrows move a
question or an option, the bin removes it. The editor sets question numbers and option letters
itself and updates them after moving.

"Add your own explanation (Додати власне пояснення)" opens the "Your own explanation (Власне
пояснення)" field below the question. You can write it straight away, before checking: the student
will see it instead of the model's explanation — even when the model has doubts. "Remove your own
explanation (Прибрати власне пояснення)" removes the field.

The limits are the same as in a file: up to 200 questions and up to 26 options per question; question
text and explanations — up to 2,000 characters, option text — up to 500.

### Save the draft {#editor-save}

"Save draft" saves the test as it is — even unfinished. There is no autosave: until you save, the
title shows "There are unsaved changes (Є незбережені зміни)", and trying to leave the page asks
"Leave without saving? (Піти без збереження?)". After saving — "All changes saved (Усі зміни
збережено)".

- **Yellow hints** show what is left to finish: "Type the question text." («Впишіть текст
  питання.»), "Type the option text." («Впишіть текст варіанта.»), "Add options: at least two are
  needed." («Додайте варіанти: потрібно щонайменше два.»), "Mark at least one correct option."
  («Позначте хоча б один правильний варіант.»). These are not errors: a draft with them is saved. You
  can check and publish the test when no yellow hints are left.
- **Red errors** are what cannot be saved: an empty or too long title, a pass mark that is not a whole
  number from 1 to 100, too long a text. The error is shown next to its field, the draft is not saved,
  and at the top — "The draft was not saved: fix what is marked in red below." («Чернетку не
  збережено: виправте позначене червоним нижче.»). If the system itself did not accept a text, its
  refusal is shown next to the question and option it named.

### Check with the model {#editor-check}

"Check with the model (Перевірити моделлю)" orders, for the saved draft, an explanation for each
question and the model's doubts about your marks. Students see none of this: the check publishes
nothing. When the button is unavailable, the reason is given next to it: "Save the test first."
(«Спершу збережіть тест.»), "Save your changes first." («Спершу збережіть зміни.»), "To check or
publish the test, finish the marked places." («Щоб перевірити чи опублікувати тест, завершіть
позначені місця.») or "The draft has already been checked." («Чернетку вже перевірено.»).

What it costs: checking a new or changed draft is one writing of explanations by the model — as much
as publishing without a check would cost. Checking an unchanged draft that is already checked or
published costs nothing. Changing the pass mark or your own explanations does not reset the check.

After you press it — "Check ordered: the model's explanations and doubts will appear next to the
questions in a minute or two." («Перевірку замовлено: пояснення й сумніви моделі з'являться біля
питань за хвилину-дві.») and the label "Checking… (Перевірка йде…)". When the explanations are ready,
the label becomes "Checked by the model (Перевірено моделлю)":

- next to each question — a "Model's explanation (Пояснення моделі)" block;
- a question whose marks the model has doubts about gets the "Model's doubt (Сумнів моделі)" label
  and a hint on what to do: check the marks; if they are correct, write your own explanation (see
  [Doubt](#doubt));
- "Use the model's text as a starting point (Взяти текст моделі за основу)" copies the model's
  explanation into your own explanation so that you can edit it; the model's explanation stays as it
  was.

If you change questions, options or marks after the check, the editor warns: "You changed questions,
options or marks — after saving, the test will need to be checked again." («Ви змінили питання,
варіанти чи позначки — після збереження тест треба буде перевірити знову.») If the check failed —
"The check failed (Перевірка не вдалася)": press "Check with the model" again.

### As the student sees it {#editor-student}

The "Edit (Правка)" · "As the student sees it (Як бачить студент)" switch shows the test as students
will see it after publishing: questions with option letters, without marks of the correct ones and
without explanations.

The labels next to the title say where the test is: "New test (Новий тест)" — not saved yet, "Not
published (Не опубліковано)" — students do not see it, "Published (Опубліковано)" — they do. "There
are unpublished changes (Є неопубліковані зміни)" — the draft differs from the version students see:
they keep seeing the previous version until you publish a new one.

![The test through the student's eyes: questions with option letters, without marks of the correct ones and without explanations (Ukrainian interface)](/assets/test-editor-student-view.png)

*"As the student sees it". Next to the title — "Published" and "There are unpublished changes":
students see the previous version until you publish this one.*

### Publish {#editor-publish}

"Publish (Опублікувати)" first asks "Publish the test? (Опублікувати тест?)" and says what will
change: the first publication — "Students of the course will see the test." («Тест побачать студенти
курсу.»), a new version — "Students will see the new version of the test. Attempts already made will
keep the grades they got." («Студенти побачать нову версію тесту. Спроби, які вже зроблено, лишаться
з тими оцінками, які отримали.»)

- **A checked draft** is published at no extra charge: "The model's explanations are already ready."
  («Пояснення моделі вже готові.»)
- **An unchecked draft** can be published too — the window warns that you have not seen the model's
  explanations and doubts yet: the explanations will appear a few minutes after publishing, and anyone
  who takes the test earlier will get a grade without them. Publishing orders the explanations by
  itself.

After publishing — "Published: students see this version of the test." («Опубліковано: студенти
бачать цю версію тесту.») Students see and take the new version straight away; what exactly creates a
new version, see [What creates a new version](#versions).

![The publish window for an unchecked draft, with a warning (Ukrainian interface)](/assets/test-editor-publish.png)

*Publishing an unchecked draft: the window warns that the model's explanations will appear only after
publishing.*

### YAML and hiding {#editor-yaml}

- "Download YAML (Вивантажити YAML)" saves the draft, together with the title, to a YAML file. The
  file contains the correct answers — do not give it to students.
- "Replace from YAML… (Замінити з YAML…)" replaces the draft with the content of a file after the
  confirmation "Replace the draft with the file? (Замінити чернетку файлом?)". The published version
  does not change until you publish the test again. The file format — see
  [How to write a test](#yaml).
- "Hide test (Приховати тест)", after the confirmation "Hide the test? (Приховати тест?)", removes the
  test from the course for students; attempts already made are kept. Hiding cannot be undone in the
  author app.

## Your own explanations {#own-explanations}

A student who answered a question wrongly sees the correct option and an explanation. If you have
written your own explanation for this question (`explanation`), the student sees **exactly that**,
not the system's explanation — even when the system has doubts about this question.

Your own explanations are shown as you wrote them and are considered to be written in the course
language: if the student reads the review in another language, the review says that the explanation
is given in the course language.

## Doubt {#doubt}

While writing explanations, the system may have doubts about your mark — for example, when a marked
option is, in its opinion, wrong. Such a question gets the "Model's doubt" label (see
[Check with the model](#editor-check)).

A doubt **does not change the score**: students' answers are still checked against your marks. But a
student who answered such a question wrongly will not see the system's explanation — only
"Incorrect", the correct option and a note that there is no explanation for this question. They will
see your own explanation.

What to do: check the marks of this question. If a mark is wrong, fix it in the draft and publish.
If it is right, add your own explanation to the question and publish: the system's explanations are
not written again. It is convenient to see the doubts before publishing by checking the draft (see
[Check with the model](#editor-check)): that way you have time to fix a mark or add an explanation
before students see the test.

## How the score is calculated {#score}

- **Every question weighs the same**, however many options it has.
- **A question counts only on an exact match**: the student marked exactly the options you marked as
  correct — no more and no fewer. An extra marked option, just like a missed correct one, makes the
  question not count. A question without an answer gives zero.
- **The score** is the share of questions that count, as a percentage, rounded down to a whole
  number: 6 questions out of 7 is 85, not 86.
- **A pass** is a score not lower than the pass mark (`pass_threshold`). Without a pass mark there is
  no verdict: the review shows only the score.

Example — a test of 7 questions, pass mark 80:

| questions that count | score | verdict |
|---|---|---|
| 7 of 7 | 100 | passed |
| 6 of 7 | 85 | passed |
| 5 of 7 | 71 | not passed |

It is convenient to set the pass mark by counting how many questions the student may get wrong. With
7 questions, pass marks of 80 and 85 give the same result: the test is passed with 6 correct answers
and not passed with 5.

## What creates a new version {#versions}

- **Questions, options or correctness marks changed** — a new version with new explanations from the
  system. If you checked this draft, the explanations are already written, and publishing takes them
  ready-made; if not, the system writes them after publishing.
- **Only the pass mark or only your own explanations changed** — a new version, but the student sees
  the same questions and options, and the system's explanations are not written again.
- **The draft is returned to an earlier state** — for example, an edit is undone — a new version, and
  it becomes the current one. The explanations the system has already written for that state are used
  again.
- **The same draft published a second time** changes nothing.

Reviews of attempts made before publishing stay as they were. A new attempt is checked against the
version that is current at the moment the student sends the answers.

A student who opened the test before publishing and answers after it: if questions or options
changed, answers to the old version are not accepted — the student re-reads the test and answers
again; if only the marks, the pass mark or your explanations changed, the answers are accepted and
checked against the new version.

## How to write a test {#yaml}

A test is written in YAML format — plain text where each field is on its own line and nesting is
shown by indentation with spaces. An example:

```yaml
title: Working with an agent — test for lesson 3
pass_threshold: 80
questions:
  - text: What is a model's context window?
    options:
      - text: The editor window in which the agent shows changes
        correct: false
      - text: The amount of text the model sees in one call
        correct: true
      - text: The time during which an agent session stays active
        correct: false
  - text: What is worth doing after each change the agent made? Choose two answers.
    options:
      - text: Run the tests
        correct: true
      - text: Delete the change history
        correct: false
      - text: Look at what exactly has changed
        correct: true
    explanation: Tests show whether the change broke what already worked.
```

| field | required | what it is |
|---|---|---|
| `title` | no | the test title students see, up to 200 characters; without it, a test uploaded as a file gets the file name without the extension |
| `pass_threshold` | no | the pass mark — a whole number of per cent from 1 to 100: what share of the questions must be answered correctly for the test to be passed (see [How the score is calculated](#score)); without a pass mark, the review shows the score but does not say "passed" or "not passed" |
| `questions` | yes | the questions, up to 200; to check and publish the test — at least one |
| question `text` | yes | the question text, up to 2,000 characters |
| `options` | yes | the answer options, up to 26; to check and publish the test — at least 2 in each question |
| option `text` | yes | the option text, up to 500 characters |
| `correct` | yes | `true` — the option is correct, `false` — it is wrong; to check and publish the test, at least one `true` in each question |
| `explanation` | no | your own explanation for the question, up to 2,000 characters; an empty one is the same as none |

- **Do not write numbers or letters.** The system numbers the questions and sets the option letters
  itself, in order: for a course in Ukrainian — `а`, `б`, `в`…, for the rest — `a`, `b`, `c`….
- **Any other field is an error**, as is an empty title. The system removes spaces at the beginning
  and end of a text.
- **An unfinished draft can be saved**: without questions (`questions: []`), with a question without
  options or with one, without a single `correct: true`, with an empty question or option text. The
  `questions`, `text`, `options` and `correct` fields are always written — only the value can be empty,
  and `correct` can only be `true` or `false`. Only a finished test can be checked and published: the
  editor shows with yellow hints what is left to finish (see [Save the draft](#editor-save)).
- **Text stays text**: the system reads `8`, `yes` or `null` in a `text` field as text. But if a text
  contains a colon or a `#` sign, or starts with something other than a letter or a digit, put it in
  double quotes, and write quotes inside such a text as `\"`: otherwise YAML will read it its own way
  — for example, it will treat everything after ` #` as a comment and drop it.
- **Start a multi-line text** with the `|` sign, and write the lines below it with extra indentation.
- A file or a request — no more than 256 KB.

### Several correct answers {#multiple-correct}

A question can have several correct options — mark each one `correct: true`. The form puts
checkboxes next to all options in every question, so the student learns how many options to mark
only from the question text. Say it in words: "Choose two answers", "Mark all correct ones".

A question counts only when **exactly** all the correct options are marked — no more and no fewer. A
question without a single mark counts as wrong.
