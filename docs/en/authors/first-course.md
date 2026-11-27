---
title: Signing in and your first course
language: en
role: authors
keywords:
  - authors
  - sign in
  - access key
  - My courses
  - new course
  - course language
last_updated: 2026-11-22
---

# Signing in and your first course

## Signing in {#login}

The Course Supporter author app works in the browser, at `panel.pythoncourse.me`. On the "Sign in (Вхід)" page, paste your access key into the "API key (API ключ)" field and press "Connect (Підключитися)".

<!-- frame 1: the "Sign in (Вхід)" page — an empty "API key (API ключ)" field, the "Connect (Підключитися)" button -->

The key is issued by the Course Supporter platform administrator; you cannot register by yourself. How to contact them — on the [home page](../index.md#authors). The key is stored in your browser until you press "Sign out (Вийти)". In another browser or on another computer, you have to enter it again.

Important: the key opens all your courses, together with the methodological materials and the correct answers of the tests. If you work at a school where several people have keys, each of them sees all the school's courses. Do not give the key to students, and make sure it does not end up in a screenshot.

If you see "Invalid API key. Check it and try again." («Невірний API ключ. Перевірте та спробуйте ще раз.»), check that the key was copied in full. If it was, the key may have been revoked or may have expired: contact the platform administrator.

One more case worth knowing about. If the key stops working after you have already signed in, the "My courses (Мої курси)" page shows "No courses yet (Ще немає курсів)" — as if you had no courses — and other pages show "API error 401". Your courses have not gone anywhere. Press "Sign out" and sign in again; if that does not help, write to the platform administrator. As of October 2026, the app does not explain this by itself.

## "My courses" and a new course {#courses}

After signing in, you are on the "My courses" page: here is a card for each of your courses and the "New course (Новий курс)" button.

<!-- frame 2: "My courses (Мої курси)" — a course card with counters, the "New course (Новий курс)" button -->

To create a course, press "New course" and fill in:

- "Course title (Назва курсу)";
- "Description (Опис)" — optional;
- "Course language (Мова курсу)" — required.

<!-- frame 3: the "New course (Новий курс)" window — "Course title (Назва курсу)" and "Course language (Мова курсу)" filled in -->

Press "Create (Створити)" — the app opens the new course straight away. From then on, it is a card on the "My courses" page: click it to open the course.

## Course language {#language}

In the course language, the platform writes section descriptions, explanations for tests and the list of criteria the Mentor uses to review assignments. The Mentor writes the review in the language the student chooses; if the student has not chosen one, in the course language. Choose the language you teach in.

It is better to choose the course language correctly right away. You can change it later — with the pencil button next to the language name on the course page — but the platform does not translate anything when you do. Materials already processed and finished section descriptions stay in the previous language, and published tests will have to be published again. Only materials uploaded after the change get the new language. When changing it, do not choose "Auto-detect (Автовизначення)" in the language list: a course needs a specific language, and such a choice will not be saved — as of October 2026, the app does not warn you about this.

## If you have your own learning system {#own-lms}

If your school or you yourself use your own learning management system (LMS), you may not need the author app at all: the Course Supporter platform can be integrated at the API level, and work review can be built straight into your system. If this interests you, contact the Course Supporter platform administrator ([how to contact them](../index.md#authors)). Some of the requests are already described — [Tests and criteria via the API](api.md).

If you work in the author app, next — [Course structure](structure.md).
