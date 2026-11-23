---
title: API
language: en
role: api
keywords:
  - api
  - swagger
  - integration
  - test
  - webhook
  - replies
last_updated: 2026-11-22
---

# API

The machine-readable specification of all routes is in the API description: [api.pythoncourse.me/openapi.json](https://api.pythoncourse.me/openapi.json).
Below is a guide for a school platform that submits students' work through the API: how to accept
answers to a test, what is checked in a student's comment, and how to accept a student's replies to a
review.

## A test as a submission {#tests}

A test is submitted not as a file but as answers to its questions. Both routes below take the school's
access key in the `X-API-Key` header with the `CHECK` scope — the same as for submitting a file. A key
without this scope gets **403** `Requires scope: check`.

A test is checked against the correct answers the author marked in the test itself (see
[For authors](../authors/tests.md#test)), and the routes below see only the version the author has
published; the review arrives by webhook, just as for a file.

### Read the test {#test-structure}

```bash
curl https://api.pythoncourse.me/api/v1/homework/tasks/$TASK_ID/test \
     -H "X-API-Key: $CHECK_KEY"
```

Response **200** — exactly three fields:

```json
{
  "version": "3f9c1e…",
  "accepting_answers": true,
  "questions": [
    {
      "number": "1",
      "text": "Що таке контекстне вікно моделі?",
      "options": [
        {"label": "а", "text": "Вікно редактора, у якому агент показує зміни"},
        {"label": "б", "text": "Обсяг тексту, який модель бачить за один виклик"}
      ]
    }
  ]
}
```

- **`version`** — the current version of the test. Send it back as `test_version` together with the
  answers.
- **`accepting_answers`** — whether answers are accepted right now. While it is `false`, a submission
  is refused with the code [`TEST_FORM_UNAVAILABLE`](#TEST_FORM_UNAVAILABLE) — show the student the
  test, but do not accept answers.
- **`questions`** — the questions and options of the published version: the number and text of each
  question, the label and text of each option. Texts are as the author wrote them; the system sets
  the numbers and labels in order — Ukrainian letters for a course in Ukrainian, lowercase Latin
  letters for the rest.

There is nothing from the key here: no correct options, no information on how many there are in a
question, no explanations. The author indicates questions with several correct answers in words in
the question text, so show a checkbox next to each option.

**404** `Task not found.` — the same response for an assignment that does not exist, a deleted one,
another school's assignment and an assignment that is not a published test: another material, or a
test the author has not published yet.

### Send the answers {#submit-test}

```bash
curl -X POST https://api.pythoncourse.me/api/v1/homework/submit-test \
     -H "X-API-Key: $CHECK_KEY" -H "Content-Type: application/json" \
     -d '{
           "student_external_id": "student-42",
           "course_node_id": "…",
           "node_id": "…",
           "authored_document_id": "…",
           "answers": {"1": ["б"], "2": ["а", "в"]},
           "test_version": "3f9c1e…",
           "response_language": "en"
         }'
```

| field | required | what it is |
|---|---|---|
| `student_external_id` | yes | the student's identifier in your system |
| `course_node_id` | yes | the course |
| `node_id` | yes | the course node the test belongs to |
| `authored_document_id` | yes | the test itself |
| `answers` | yes | question number → labels of the marked options |
| `test_version` | no | the `version` returned by reading the test |
| `webhook_url` | no | the webhook address for this submission; without it — the school's default address |
| `response_language` | no | the review language, an ISO 639-1 or 639-3 code |
| `student_note` | no | the student's comment on the attempt, up to 2,000 characters (see [A student's comment](#student-note)) |

**Answers.** Send each label exactly as the structure route showed it (`label`) — with the same
letters and in the same case. A question without an answer is not refused — it counts as wrong. A
question or label that the test does not have is refused with
[`ANSWERS_DO_NOT_MATCH_TEST`](#ANSWERS_DO_NOT_MATCH_TEST).

**Test version.** `test_version` is optional. If it is sent and is not the current one, the refusal is
[`TEST_VERSION_CHANGED`](#TEST_VERSION_CHANGED): the test changed while the student was answering.
Without it, the answers are matched against the current version.

**202** — the submission is accepted:

```json
{"submission_id": "…", "student_id": "…", "status": "received", "job_id": "…", "duplicate": false}
```

The same answers sent twice are two attempts: each is checked and gets its own review.

The course, node and assignment are checked the same way as when submitting a file: **404** for
whatever your school does not have — and the same **404** `Task not found.` for an assignment that is
not a published test. Then come the test's own checks, each with its own code (see
[Refusals of a test submission](#test-refusals)).

A file sent to a test assignment by the file submission route (`POST /api/v1/homework/submit`) is
refused with the code [`TEST_ANSWERS_REQUIRED`](#TEST_ANSWERS_REQUIRED), and one sent to a test the
author has not published yet gets **404** `Task not found.`, as for a missing assignment.

### The review in the webhook {#webhook}

The review of a test is delivered by the `reviewed` event — the same as for a file, with the same
fields. For a test, they mean the following:

| field | for a test |
|---|---|
| `review.score` | the share of correct answers, a whole number of per cent, rounded down |
| `review.passed` | whether the pass mark is reached; if the author did not set a pass mark — `true`: there is no bar that can be missed |
| `review.correctness` | from the score: `100` → `correct`, `0` → `incorrect`, otherwise → `partially_correct` |
| `review.review_text` | the review as text (Markdown) — you can show it to the student as it is |
| `structure.test` | the same by question: `number`, `correct`, `correct_answer` (the label and text of the correct options), `explanation`; and also `score`, `explanations_in_course_language`, `retry_offer` |

If the review could not be put together, a `failed` event arrives with the reason in `reason`:

#### `path_failed` {#path_failed}

The submission could not be reviewed because of a failure on our side, and there will be no review of
it. Offer the student to send the answers again.

#### `test_not_ready` {#test_not_ready}

**Does not arrive for tests created in the system**: a submission is checked against the version of
the test it was accepted for, and a published version always has correct answers. Previously, this
reason meant that, while the submission was waiting to be checked, the author removed or changed the
answer key. If your platform already handles it, you can keep the handling: tell the student the test
is temporarily not accepting answers, and offer to try later.

## Refusals of a test submission {#test-refusals}

A refusal arrives before anything is saved: no submission is created, nothing is recorded. The
`detail` field has the reason code and an explanation, `{"code": "…", "details": "…"}`. The code is
for your code; pick the words for the student yourself by the code, and `details` is a fallback text
for a code your platform does not know yet.

The checks go in this order: whether the test accepts answers at all → whether it is the right version
→ whether the questions and labels match → the student's comment (see [A student's comment](#student-note)).
So a submission with several defects gets the code of the first one.

#### `TEST_ANSWERS_REQUIRED` {#TEST_ANSWERS_REQUIRED}

**422.** A file was sent to a test. A test is taken with answers: send them by the
[`POST /api/v1/homework/submit-test`](#submit-test) route.

#### `TEST_FORM_UNAVAILABLE` {#TEST_FORM_UNAVAILABLE}

**409.** Tests are temporarily not accepting answers. A test created in the system cannot be taken as a
file either, so tell the student the test is temporarily unavailable, and try later — when reading the
test shows `accepting_answers: true` again.

#### `NOT_A_TEST_TASK` {#NOT_A_TEST_TASK}

**422.** **Not issued for tests created in the system**: for an assignment that is not a published
test, the test routes respond **404** `Task not found.`. Previously, this code meant the assignment was
not a test. If your platform already handles it, you can keep the handling: send the work as a file or
check that it is really the test assignment that is specified.

#### `TEST_VERSION_CHANGED` {#TEST_VERSION_CHANGED}

**409.** The answers were given to a version of the test that is no longer current: the author changed
the test while the student was answering. Read the test again, show the student the new version and
send the answers to it.

#### `TEST_NOT_READY` {#TEST_NOT_READY}

**409.** **Not issued for tests created in the system**: a published version of a test always has
correct answers, and the routes do not see an unpublished test at all. Previously, this code meant
there was no answer key for the current version of the test. If your platform already handles it, you
can keep the handling: tell the student the test is not ready yet, and try later.

#### `ANSWERS_DO_NOT_MATCH_TEST` {#ANSWERS_DO_NOT_MATCH_TEST}

**422.** The answers name a question or option that the test does not have; `details` lists them.
Compare the answers with the reading of the test — question numbers and option labels — and send them
again.

## A student's comment {#student-note}

To a file submission (`POST /api/v1/homework/submit`, the `student_note` form field) and to answers to
a test (`POST /api/v1/homework/submit-test`, the `student_note` field), the student can add a comment —
a question or a clarification about the work. The comment is checked before anything is saved:

- **length** — up to 2,000 characters; a line break counts as one character, spaces at the beginning
  and end are not counted and not saved;
- **text checks** — the same as for text files of a submission. Only characters that never occur in
  legitimate text cause a refusal: text direction changes, tag and control characters. Zero-width
  invisible spaces and phrases resembling instructions to the review system do not get a comment
  rejected: they are flagged and assessed separately by the model-based security check of the
  submission.

A comment that did not pass is refused together with the submission: nothing is saved — no
submission, no file, no answers. Show the student the reason, let them fix the comment and send the
submission again. A submission without a comment is not affected by these checks. In a file
submission, the comment is checked last, after the file and the assignment; in a test submission —
after the answers.

#### `STUDENT_NOTE_TOO_LONG` {#STUDENT_NOTE_TOO_LONG}

**422.** The comment is longer than 2,000 characters; `details` gives its length. Ask the student to
shorten the comment.

#### `STUDENT_NOTE_REJECTED` {#STUDENT_NOTE_REJECTED}

**422.** The comment did not pass the text check. `details` starts with the check category and a
colon:

| category | what is in the comment |
|---|---|
| `suspicious_unicode` | text direction characters, tag or control characters; they often come with text copied from another program |
| `charset_violation` | a string that is not valid Unicode — for example, a lone surrogate `\ud800` in JSON |

Ask the student to rewrite the comment without this.

## A student's replies {#replies}

**Turned off for now.** The routes below respond as if there were no replies: sending — **404**
`Submission not found.`, reading — an empty list of threads and `accepting_replies: false`. They will
be turned on together with answers to replies in the next review.

A student can reply to a comment in their review — with a question, an objection or a comment — and the
next review of their work will answer every reply. Only the work changes the grade: a reply does not
change the verdict by itself, whatever it says. Both routes take the school's key in the `X-API-Key`
header with the `CHECK` scope — the same as for a submission.

### Send a reply {#reply-post}

```bash
curl -X POST https://api.pythoncourse.me/api/v1/feedback/submissions/$SUBMISSION_ID/comments \
     -H "X-API-Key: $CHECK_KEY" -H "Content-Type: application/json" \
     -d '{
           "student_external_id": "student-42",
           "item_id": "c3",
           "kind": "question",
           "body": "Чому критерій 3 не виконано? Перевірка порожнього списку є в рядку 12.",
           "parent_id": null
         }'
```

| field | required | what it is |
|---|---|---|
| `student_external_id` | yes | the student's identifier in your system; this route does not create a student |
| `item_id` | yes | the review comment: `c3` — a criterion, `c3.p2` — one of its points |
| `kind` | yes | `question` — a question, `objection` — an objection, `comment` — a comment |
| `body` | yes | the text of the reply, up to 2,000 characters |
| `parent_id` | no | the comment in the same thread this is an answer to; without it — an answer to the review comment itself |

**201** — the reply is saved:

```json
{
  "id": "…",
  "submission_id": "…",
  "item_id": "c3",
  "parent_id": null,
  "author": "student",
  "kind": "question",
  "body": "Чому критерій 3 не виконано? Перевірка порожнього списку є в рядку 12.",
  "created_at": "2026-11-16T10:00:00Z",
  "answered_in_submission_id": null
}
```

`answered_in_submission_id` — the submission whose review answered the reply; while none has answered
it — `null`.

**Which review can be replied to.** A reply is answered by the student's next submission for this
assignment, so it is accepted only by the latest reviewed attempt: the review is written; no later
submission by the student for this assignment has been reviewed or is being reviewed (a later one that
was rejected or could not be reviewed does not get in the way); the review concerns the current
version of the assignment. Otherwise — the refusal [`not_latest_review`](#not_latest_review).

**Limit.** Up to 5 student replies per comment between two submissions. A new submission opens a new
limit — for the comments of its review.

**The text** is checked the same way as [a student's comment](#student-note) on a submission: line
breaks count as one character, spaces at the beginning and end are not counted and not saved; text
direction, tag and control characters cause a refusal. Phrases resembling instructions to the review
system do not get a reply rejected: they are assessed separately by the security check, and the review
answers such a reply without its text.

### Read the threads {#reply-get}

```bash
curl "https://api.pythoncourse.me/api/v1/feedback/submissions/$SUBMISSION_ID/comments?student_external_id=student-42" \
     -H "X-API-Key: $CHECK_KEY"
```

**200** — the comment threads of this review:

```json
{
  "accepting_replies": true,
  "threads": [
    {"item_id": "c3", "comments": [{"id": "…", "author": "student", "kind": "question", "…": "…"}]}
  ]
}
```

**`accepting_replies`** — whether this review will accept a reply right now (the assignment accepts
replies, and this is the student's latest reviewed attempt on the current version of the assignment):
while it is `false`, do not offer the student to write a reply — sending will be refused.

A comment's thread runs through all the student's attempts on the same version of the assignment:
their reply to one review, the answer in the next one, their answer to the answer. Comments are in
time order, nesting is via `parent_id`; the author is `student` or `mentor`. A review comment without
a single reply has no thread.

### Refusals of replies {#reply-refusals}

**404** `Submission not found.` — the same response for a submission that does not exist, a submission
of another student or another school, a deleted submission, an unknown `student_external_id` — and for
an assignment that does not accept replies (for now — every one). Reading the threads of such an
assignment gives an empty list and `accepting_replies: false`.

Next — **422** with the `detail` field in the form `{"code": "…", "details": {…}}`. Unlike other
refusals, `details` here is an object: empty or with the refusal's parameters. The checks go in this
order: where the reply goes — [`not_latest_review`](#not_latest_review) →
[`unknown_item`](#unknown_item) → [`parent_not_in_thread`](#parent_not_in_thread) →
[`comment_limit_reached`](#comment_limit_reached); then the text —
[`comment_empty`](#comment_empty) → [`comment_too_long`](#comment_too_long) →
[`comment_rejected`](#comment_rejected). So a reply with several defects gets the code of the first
one.

#### `not_latest_review` {#not_latest_review}

**422.** This review is no longer the one the next submission will answer: there is a later
submission that has been reviewed or is being reviewed, the review is not written yet, or the
assignment has changed since it. Show the student the latest review — a reply can be written to it.

#### `unknown_item` {#unknown_item}

**422.** The review has no comment with this `item_id`. Check the comment's identifier.

#### `parent_not_in_thread` {#parent_not_in_thread}

**422.** `parent_id` does not name a comment from this review comment's thread. Read the threads again
and answer a comment from them — or send the reply without `parent_id`.

#### `comment_limit_reached` {#comment_limit_reached}

**422.** There are already 5 student replies to this comment; `details.max` is `5`. Further replies
can be written to the review of the next submission.

#### `comment_empty` {#comment_empty}

**422.** The reply is empty or consists only of spaces.

#### `comment_too_long` {#comment_too_long}

**422.** The reply is longer than 2,000 characters; `details.max` is `2000`. Ask the student to shorten
it.

#### `comment_rejected` {#comment_rejected}

**422.** The reply did not pass the text check; `details.category` is the category:
`suspicious_unicode` (text direction characters, tag or control characters) or
`charset_violation` (a string that is not valid Unicode). Ask the student to rewrite the reply without
them.
