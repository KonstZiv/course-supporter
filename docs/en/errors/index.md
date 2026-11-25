---
title: Error codes
language: en
role: reference
error_codes:
  - TEST_ANSWERS_REQUIRED
  - TEST_FORM_UNAVAILABLE
  - NOT_A_TEST_TASK
  - TEST_VERSION_CHANGED
  - TEST_NOT_READY
  - ANSWERS_DO_NOT_MATCH_TEST
  - STUDENT_NOTE_TOO_LONG
  - STUDENT_NOTE_REJECTED
  - not_latest_review
  - unknown_item
  - parent_not_in_thread
  - comment_limit_reached
  - comment_empty
  - comment_too_long
  - comment_rejected
  - path_failed
  - test_not_ready
  - review_parts_missing
  - TEST_YAML_UNREADABLE
  - TEST_YAML_DUPLICATE_KEY
  - TEST_YAML_ALIAS
  - TEST_FIELD_INVALID
  - TEST_OPTIONS_COUNT
  - TEST_TOO_LARGE
  - SECURITY_REJECTED
  - NOT_A_TEST_OBJECT
  - TEST_DRAFT_INCOMPLETE
  - GENERATION_IN_PROGRESS
  - TEST_FILE_NOT_YAML
  - TEST_OBJECT_SOURCE_RESERVED
  - TEST_OBJECT_TYPE_FIXED
  - TEST_OBJECT_ROLE_FIXED
  - TEST_IS_AN_OBJECT
  - TEST_OBJECT_NOT_PROCESSED
  - KEY_LIVES_IN_TEST
  - TEST_NO_QUESTIONS
  - TEST_TEXT_EMPTY
  - TEST_NO_CORRECT_OPTION
  - TASK_NOT_READY
  - TASK_LANGUAGE_UNSET
  - TASK_TEXT_TRUNCATED
  - NO_QUESTION_NUMBERS
  - KEY_DOES_NOT_MATCH_QUESTIONS
  - NOT_A_TEXT_TASK
  - AWAITING_FIRST_SUBMISSION
  - UNKNOWN_CRITERION_ID
  - CHECK_METHOD_NOT_ADMITTED
  - UNKNOWN_CONCEPT
  - MUST_BY_AUTHOR_WITHOUT_MUST
keywords:
  - errors
  - error codes
  - test
  - YAML
  - comment
  - criteria
  - replies
last_updated: 2026-11-23
---

# Error codes

An API refusal carries the reason code and an explanation in the `detail` field:
`{"code": "…", "details": "…"}`. The code is a stable key by which your platform picks its own
wording; `details` is a fallback text for a code it does not know yet. A code with a link leads to
the section where it is described in detail.

While the service is being updated (a few minutes), any route may answer **503** with the body
`{"detail": "service_updating"}` and a `Retry-After` header (seconds until the retry, currently
`15`). This is not a refusal of the request: repeat it after that time, unchanged.

## Submitting answers to a test

Codes the school's platform gets when sending a student's answers. Nothing from a refused submission
is saved.

| code | status | reason | what to do |
|---|---|---|---|
| [`TEST_ANSWERS_REQUIRED`](../api/index.md#TEST_ANSWERS_REQUIRED) | 422 | a file was sent to a test | send the answers by the test submission route |
| [`TEST_FORM_UNAVAILABLE`](../api/index.md#TEST_FORM_UNAVAILABLE) | 409 | tests are temporarily not accepting answers | tell the student the test is temporarily unavailable, and try later |
| [`NOT_A_TEST_TASK`](../api/index.md#NOT_A_TEST_TASK) | 422 | **not issued for tests created in the system**: an assignment that is not a published test gets 404 | if the platform already handles the code — check that it is really the test assignment that is specified |
| [`TEST_VERSION_CHANGED`](../api/index.md#TEST_VERSION_CHANGED) | 409 | the test changed while the student was answering | read the test again and answer the new version |
| [`TEST_NOT_READY`](../api/index.md#TEST_NOT_READY) | 409 | **not issued for tests created in the system**: a published version always has correct answers | if the platform already handles the code — try later |
| [`ANSWERS_DO_NOT_MATCH_TEST`](../api/index.md#ANSWERS_DO_NOT_MATCH_TEST) | 422 | a question or option is named that the test does not have | compare the answers with the reading of the test and send the labels exactly as the structure route showed them |

## A student's comment on a submission

Codes the school's platform gets when a student's comment (`student_note`) added to a submission — a
file or answers to a test — did not pass the checks (see [A student's comment](../api/index.md#student-note)).
Nothing from a refused submission is saved.

| code | status | reason | what to do |
|---|---|---|---|
| [`STUDENT_NOTE_TOO_LONG`](../api/index.md#STUDENT_NOTE_TOO_LONG) | 422 | the comment is longer than 2,000 characters | ask the student to shorten the comment and send the submission again |
| [`STUDENT_NOTE_REJECTED`](../api/index.md#STUDENT_NOTE_REJECTED) | 422 | the comment has text direction characters, tag or control characters, or a string that is not valid Unicode; `details` starts with the category | ask the student to rewrite the comment without them and send the submission again |

## Replies to a review

Codes the school's platform gets when sending a student's reply to a comment in a review (see
[A student's replies](../api/index.md#replies)). **Not issued yet**: replies are turned off, and the
route answers **404**, as for a missing submission. A refused reply is not saved. Unlike other
refusals, `details` here is an object.

| code | status | reason | what to do |
|---|---|---|---|
| [`not_latest_review`](../api/index.md#not_latest_review) | 422 | the review is no longer the latest reviewed one, is not written yet or concerns an old version of the assignment | show the student the latest review and reply to it |
| [`unknown_item`](../api/index.md#unknown_item) | 422 | the review has no such comment | check `item_id` |
| [`parent_not_in_thread`](../api/index.md#parent_not_in_thread) | 422 | `parent_id` is not from this comment's thread | read the threads again or send without `parent_id` |
| [`comment_limit_reached`](../api/index.md#comment_limit_reached) | 422 | there are already 5 student replies to this comment; `details.max` is `5` | write to the review of the next submission |
| [`comment_empty`](../api/index.md#comment_empty) | 422 | the reply is empty | ask the student to write some text |
| [`comment_too_long`](../api/index.md#comment_too_long) | 422 | the reply is longer than 2,000 characters; `details.max` is `2000` | ask the student to shorten the reply |
| [`comment_rejected`](../api/index.md#comment_rejected) | 422 | text direction characters, tag or control characters, or a string that is not valid Unicode; `details.category` is the category | ask the student to rewrite the reply without them |

## The `failed` event in the webhook

The reason is in the `reason` field when an accepted submission could not be reviewed.

| reason | what happened | what to do |
|---|---|---|
| [`path_failed`](../api/index.md#path_failed) | a failure on our side; there will be no review | offer the student to send the answers again |
| [`test_not_ready`](../api/index.md#test_not_ready) | **does not arrive for tests created in the system** | if the platform already handles the reason — offer to try later |
| `review_parts_missing` | **does not arrive today**: it will appear for text assignments when they are reviewed by per-criterion verdicts; it will mean the review could not be put together because of a failure on our side, and there will be none | tell the student the work could not be reviewed through our fault and offer to send it again; if the reason keeps recurring — inform the system administrator |

## A test created in the system

Codes the author gets when creating, changing, checking and publishing a test. Nothing from a refused
request is saved. Format refusals also carry `place` — the place of the error, and a refusal of an
unfinished draft carries the `incomplete` list (see [When a test is refused](../authors/api.md#refusals)).

| code | status | reason | what to do |
|---|---|---|---|
| [`TEST_YAML_UNREADABLE`](../authors/api.md#TEST_YAML_UNREADABLE) | 422 | the YAML or JSON cannot be read | fix the entry at the place named by `place` |
| [`TEST_YAML_DUPLICATE_KEY`](../authors/api.md#TEST_YAML_DUPLICATE_KEY) | 422 | the same field twice in one place | keep one |
| [`TEST_YAML_ALIAS`](../authors/api.md#TEST_YAML_ALIAS) | 422 | a YAML anchor or alias (`&`, `*`) | write the text out in full |
| [`TEST_FIELD_INVALID`](../authors/api.md#TEST_FIELD_INVALID) | 422 | a field is unknown or missing, the title is empty, a field is of the wrong kind or too long | fix the field according to `details` and the format table |
| [`TEST_OPTIONS_COUNT`](../authors/api.md#TEST_OPTIONS_COUNT) | 422 | a question has more than 26 options | split the question into several |
| [`TEST_TOO_LARGE`](../authors/api.md#TEST_TOO_LARGE) | 413 | the file or request is over 256 KB | split the test into several |
| [`SECURITY_REJECTED`](../authors/api.md#SECURITY_REJECTED) | 400 | invisible characters, instruction-like phrases, or a file that could not be read as text | type the text again or save the file in UTF-8 |
| [`NOT_A_TEST_OBJECT`](../authors/api.md#NOT_A_TEST_OBJECT) | 422 | the identifier does not lead to a test created in the system | check the identifier; create an old text test again from YAML |
| [`TEST_DRAFT_INCOMPLETE`](../authors/api.md#TEST_DRAFT_INCOMPLETE) | 422 | checking or publishing an unfinished draft; `incomplete` names every place left to finish | finish every place from the list, save the draft and repeat |
| [`GENERATION_IN_PROGRESS`](../authors/api.md#GENERATION_IN_PROGRESS) | 409 | the system is writing explanations for the test right now | wait until it finishes and repeat the check or publication |
| [`TEST_FILE_NOT_YAML`](../authors/api.md#TEST_FILE_NOT_YAML) | 422 | a material of the "Test" kind is not a YAML file | write the test in YAML and upload it as a file |
| [`TEST_OBJECT_SOURCE_RESERVED`](../authors/api.md#TEST_OBJECT_SOURCE_RESERVED) | 422 | the request itself specified the source type `test_object` | do not specify it: upload a YAML file or create the test with a request |
| [`TEST_OBJECT_TYPE_FIXED`](../authors/api.md#TEST_OBJECT_TYPE_FIXED) | 422 | an attempt to change the assignment type of a test | create a new material |
| [`TEST_OBJECT_ROLE_FIXED`](../authors/api.md#TEST_OBJECT_ROLE_FIXED) | 422 | an attempt to make a test a methodological material: a test is always educational | keep the role educational; to remove the test from students — hide it |
| [`TEST_IS_AN_OBJECT`](../authors/api.md#TEST_IS_AN_OBJECT) | 422 | an attempt to turn an existing material into a test | create the test from YAML |
| [`TEST_OBJECT_NOT_PROCESSED`](../authors/api.md#TEST_OBJECT_NOT_PROCESSED) | 422 | repeated processing or file roles for a test | change the draft and publish it |
| [`KEY_LIVES_IN_TEST`](../authors/api.md#KEY_LIVES_IN_TEST) | 422 | replacing or resetting the answer key for a test | change the marks in the draft and publish it |

The system saves an unfinished draft, and names the places left to finish with the `incomplete` list —
in reading the draft and in the `TEST_DRAFT_INCOMPLETE` refusal. Each place is a code, a question
number and an option number (see [An unfinished draft](../authors/api.md#incomplete)).

| code | place | what is wrong | what to do |
|---|---|---|---|
| [`TEST_NO_QUESTIONS`](../authors/api.md#TEST_NO_QUESTIONS) | test | there are no questions | add questions |
| [`TEST_TEXT_EMPTY`](../authors/api.md#TEST_TEXT_EMPTY) | question or option | empty text | write the text |
| [`TEST_OPTIONS_COUNT`](../authors/api.md#TEST_OPTIONS_COUNT) | question | fewer than two options | add options |
| [`TEST_NO_CORRECT_OPTION`](../authors/api.md#TEST_NO_CORRECT_OPTION) | question | no option is marked as correct | mark at least one `correct: true` |

## Answer key requests

For a test created in the system, answer key requests (`…/reference/override`) are refused with the
code [`KEY_LIVES_IN_TEST`](../authors/api.md#KEY_LIVES_IN_TEST). The codes below are given by these
requests for a test uploaded as text before tests created in the system appeared, and for a material
that is not a test. A test uploaded as text no longer accepts answers — create it again from YAML (see
[How to create a test](../authors/api.md#create)). Nothing from a refused request is saved.

| code | status | reason | what to do |
|---|---|---|---|
| `NOT_A_TEST_TASK` | 422 | the assignment is not a test; **not issued for tests created in the system** | check that the assignment was created as a test |
| `TASK_NOT_READY` | 422 | the assignment is still being processed | wait for processing to finish and send the key again |
| `TASK_LANGUAGE_UNSET` | 422 | the material has no language set | set the material's language and send the key again |
| `TASK_TEXT_TRUNCATED` | 422 | the test text is too long and has been cut off | shorten the test or split it into several |
| `NO_QUESTION_NUMBERS` | 422 | the text has no question numbers at all | type the numbers `1.` at the start of the lines by hand |
| `KEY_DOES_NOT_MATCH_QUESTIONS` | 422 | the numbers in the key do not match the test | fix the key according to `details` and send it as a whole |
| `GENERATION_IN_PROGRESS` | 409 | the system is still writing explanations or processing the assignment | wait until it finishes and send the key again |

## List of criteria requests

Codes given by the requests for the list of criteria of a text assignment (`…/criteria` and
`…/criteria/override`, see [Criteria of a text assignment](../authors/api.md#criteria)). Nothing from a
refused request is saved.

| code | status | reason | what to do |
|---|---|---|---|
| [`NOT_A_TEXT_TASK`](../authors/api.md#NOT_A_TEXT_TASK) | 422 | the material is a test or not an assignment | for a test — the marks of the correct answers in the test editor |
| [`TASK_NOT_READY`](../authors/api.md#TASK_NOT_READY) | 422 | the assignment is still being processed or has been removed from the course | wait for processing to finish and repeat the request |
| [`AWAITING_FIRST_SUBMISSION`](../authors/api.md#AWAITING_FIRST_SUBMISSION) | 422 | there is no list yet | read the list: `status` and `message` say why there is none and what to do (see [When there is no list](../authors/api.md#criteria-none)); edit when `status` is `ready` |
| [`UNKNOWN_CRITERION_ID`](../authors/api.md#UNKNOWN_CRITERION_ID) | 422 | a criterion or point `id` that is not in the current list | read the list again; a new criterion — without `id` |
| [`CHECK_METHOD_NOT_ADMITTED`](../authors/api.md#CHECK_METHOD_NOT_ADMITTED) | 422 | `code_test` not in a project | choose `model_verdict` or `mandatory_points` |
| [`UNKNOWN_CONCEPT`](../authors/api.md#UNKNOWN_CONCEPT) | 422 | a concept not from the `concepts` of reading | choose a concept from `concepts` or remove it |
| [`MUST_BY_AUTHOR_WITHOUT_MUST`](../authors/api.md#MUST_BY_AUTHOR_WITHOUT_MUST) | 422 | `must_by_author: true` in a criterion whose weight is not `must` | set the weight to `must` or remove `must_by_author` |
