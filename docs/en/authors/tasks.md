---
title: Assignments
language: en
role: authors
keywords:
  - authors
  - assignment
  - assignment type
  - project
  - starter archive
  - list of criteria
  - methodological guide
last_updated: 2026-11-22
---

# Assignments

An assignment on the Course Supporter platform is an ordinary material that you gave a type in the "Document type (Тип документа)" window. The type determines what the student submits and who reviews it: a test is checked by the system itself, and all other work is reviewed by the Mentor — the platform's program.

In tooltips, the app sometimes says "kind of assignment" («вид завдання») — it is the same as the assignment type.

## Assignment types {#types}

| Type in the window | What the student submits | Who reviews it |
|---|---|---|
| "Not an assignment (Не завдання)" | nothing: it is an ordinary material | — |
| "Test (Тест)" | answers to questions right in the portal | the system, against your marks of the correct answers |
| "Short assignment (Коротке завдання)" | one file | the Mentor |
| "Assignment (Завдання)" | one file | the Mentor |
| "Project (Проєкт)" | an archive with a whole project, up to 100 MB | the Mentor |

<!-- frame 15: the "Document type (Тип документа)" window — "Educational (Навчальний)" and "Assignment (Завдання)" chosen; all types with tooltips are visible -->

Next to the types in the window there are tooltips about the amount of work — "5–10 хв, quiz" (5–10 min, quiz), "20–60 хв" (20–60 min), "мульти-крок" (multi-step), "1–2 тижні" (1–2 weeks). They appear only when you hover over the button, and they say nothing about what the student submits or who reviews it — hence the table above.

The student submits a "Short assignment" and an "Assignment" in the same way: one file. A submitted piece of work is up to 10 MB; a single file with plain text or code is no more than 1 MB. The only difference is how detailed the list of criteria the Mentor uses to review the work is: for a short assignment — a few main requirements, for an assignment — also the completeness and quality of the solution. Choose "Short assignment" for a small exercise, and "Assignment" for multi-step work.

## Worth knowing before uploading {#before-upload}

- **The type is chosen once.** After uploading, the app does not show it and does not let you change it. You can tell only two types: a project has a line about the starter archive below it, and a test has an "Open test (Відкрити тест)" button. This matters: a material that got an assignment type by mistake becomes, for the student, an assignment with a work submission form. If you made a mistake, delete the material and upload it again with the right type (units will be charged again for the new processing — see [Who pays for processing](materials.md#cost)), or write to the Course Supporter platform administrator ([how to contact them](../index.md#authors)): they can change the type without processing it again. The type of a test cannot be changed: a test can only be hidden. This is how the platform works as of October 2026.
- **Each file is a separate assignment.** The basic and advanced levels are two files and two assignments.
- **An assignment has to be an educational material.** The student will not see an assignment with the "Methodological (Методичний)" role. About roles — [Materials](materials.md#role).
- **The assignment text is the content of the file itself.** There is no separate field for the brief, so the file must say what exactly to submit and in what form.
- **A test from a file is accepted only in YAML format.** For any other file with the "Test" type, the app says: "A test is uploaded only as a YAML file (.yaml or .yml). Choose another kind of assignment or upload a YAML file." («Тест завантажується лише файлом YAML (.yaml чи .yml). Оберіть інший вид завдання або завантажте файл YAML.») It is simpler to create a test with the "New test (Новий тест)" button — see [Tests](tests.md).
- **When the student can submit work.** When the assignment is in the "Done (Готово)" state. A test also has to be published, and a project with a starter archive needs at least one version of the archive to be ready.

## How your requirements reach the Mentor {#requirements}

The Mentor does not read methodological materials directly. For it to know your review requirements:

1. Put the review guide in the same section as the assignment, with the "Methodological" role.
2. Run the Methodist ([Section descriptions: the Methodist](methodist.md)). It will read the guide and the assignment in full and carry the requirements over into the section description.
3. Review the section description. For the "Short assignment" and "Assignment" types, the Mentor reads only the "Description (Опис)" field and the core concepts of the section and the course, so requirements and allowances that matter for the review must be in the assignment text and in the "Description" field. For the "Project" type, the Mentor reads five fields: "Title (Заголовок)", "Description", "Learning objectives (Навчальні цілі)", "Success criteria (Критерії успіху)" and "Common mistakes (Типові помилки)". As of November 2026, it does not read the "Assessment approach (Підхід до оцінювання)" field, so add the requirements and allowances of a project to "Success criteria" and "Common mistakes".

A guide the Methodist has not read, because you did not run it, will not be taken into account by the Mentor. The overall picture — who reads what — is shown on the page [How the platform understands your course](how-it-works.md#who-reads).

A section has one description, so all assignments of a section — for example, basic and advanced level — share it: the Methodist marks in the description which assignment each point applies to. After the Methodist, check that the points are assigned to the right assignment. There is no separate field for a note on one assignment in the app. What to write in the guide, what not to do and what happens without it — in the subsection [Methodological material: your review requirements](materials.md#methodical).

## The project and the starter archive {#project}

A project is an assignment in which the student submits an archive with a whole project. You can attach a **starter archive** to a project — the starting project the student begins work from. Then the Mentor sees what exactly the student changed and added. A project without a starter archive is also possible — then all of the student's work counts as new.

In the section panel, below the project row, there is "No starter archive attached." («Базовий архів не прикріплено.») and the "Attach starter archive (Прикріпити базовий архів)" button. A `.zip`, `.tar.gz`, `.tgz` or `.gz` archive of up to 100 MB will do.

<!-- frame 16: a project in the section panel — "Starter archive, version 1 (Базовий архів, версія 1)", state "Done (Готово)", buttons "Manifest (Маніфест)" and "Again (Повторно)" -->

After uploading, a state appears next to it: "Normalising… (Нормалізація…)", then "Done" or "Error (Помилка)". The "Manifest (Маніфест)" button shows which files went into the base and which were excluded and why — in the "Included (Включено)" and "Excluded (Виключено)" groups. The "Again (Повторно)" button uploads a new version of the archive.

<!-- frame 17: the "Manifest (Маніфест)" window of a starter archive — groups "Included (Включено)" and "Excluded (Виключено)" -->

A new version does not cancel the previous ones: work that students have already submitted stays tied to its version, and the student will see that the base has been updated. Students always get the latest ready version.

Important: if a starter archive is attached but none of its versions is in the "Done" state yet — preparation is still going on or an error occurred — students will not be able to submit the project. Upload a version that processes successfully.

If the starter archive could not be prepared, the app shows the cause as a technical line in English — as of October 2026, this is how it is. Check that it really is a `.zip`, `.tar.gz`, `.tgz` or `.gz` archive, that it is not damaged and that it is no larger than 200 MB unpacked; create the archive again and press "Again". If that does not help, send this line to the platform administrator. An archive larger than 100 MB is rejected by the app straight away, in Ukrainian: "The archive is too large — the limit is 100 MB." («Архів завеликий — ліміт 100 МБ.»)

## The assignment's list of criteria {#criteria}

The Mentor reviews work on an assignment against a list of criteria. The platform puts the list together itself — from the assignment text, the section's "Description" field and the core concepts of the section and the course.

It does so when the first piece of work arrives for the assignment. After that, all work on this assignment, from all students, is reviewed against the same list.

Important: an edit of the section's "Description" field after the first piece of work is submitted no longer reaches the list. So get the "Description" of a section with an assignment right before students — or you yourself, on behalf of a test student — start submitting work. The Mentor does not read the other description fields for the "Short assignment" and "Assignment" types, and for the "Project" type it reads them anew in every review, so you can refine them later too (more — [How the platform understands your course](how-it-works.md#summary)).

**Coming soon!** It is planned that the "Learning objectives", "Success criteria" and "Common mistakes" fields will become the basis of the list of criteria for the "Short assignment" and "Assignment" types as well, so requirements written in these fields will make it into the review.

The platform will put together a new list if the assignment itself changes — its text or type.

You cannot see or correct the list in the author app yet: there is no screen for it — as of October 2026. It can only be done with API requests, described on the page [Tests and criteria via the API](api.md). If you need to see or correct the list, write to the Course Supporter platform administrator.

## What the Mentor does with a piece of work {#review}

As of November 2026, this is how work of the "Short assignment" and "Assignment" types is reviewed. A project is reviewed by the Mentor in its earlier form: it is described above and on the page [How the platform understands your course](how-it-works.md#summary).

First, the platform runs a security check on the work, and on the first submission it also checks whether the work looks like an attempt at this particular assignment. Then the Mentor assesses the work against each criterion of the list separately.

For each criterion that is not met, the review gives a comment in three parts: how it is done now, what the problem is, how it should be. In addition, up to three links to places in your course where this topic is explained can be added to a criterion: a time in a video or audio (minutes and seconds), a slide, a paragraph or a file. The platform picks the links by the criterion's core concepts, so a criterion whose concepts did not match the course's concepts will have no links. Only educational materials in the "Done" state go into the links; assignments, tests and service files do not.

**Whether the work is passed.** Each criterion in the list has a weight: a required criterion weighs 3, a desirable one 2, an optional one 1. The work's score is the share of the weights of the criteria met, as a percentage, rounded down; only work in which everything is done gets 100. If the list of criteria was put together by the platform, the work is passed when the score is at least 80. The review tells the student the score, the pass mark and the rule by which the decision was made. A required criterion set by the platform is assessed, shown in the review and counts in the score, but it does not block passing by itself.

The rule is different for a list you saved yourself that has at least one required criterion (the list can be corrected only with API requests): then the work is passed when every required criterion is met and the score is at least 70. Details — on the page [Tests and criteria via the API](api.md#criteria-pass).

If a student submits the same work again for the same assignment, and the assignment, the list of criteria and the Mentor itself have not changed in the meantime, they get the review of their previous attempt — the platform does not review it again; the review starts with a note that the work has not changed. When we update the Mentor (change its instructions, model or review rules), the same work is reviewed again, and the result may differ from the previous one. To see a new review on behalf of a test student, change the work.

Next — [Tests](tests.md).
