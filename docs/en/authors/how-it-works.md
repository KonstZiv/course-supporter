---
title: How the platform understands your course
language: en
role: authors
keywords:
  - authors
  - Methodist
  - Mentor
  - educational material
  - methodological material
  - section description
  - material processing
last_updated: 2026-11-22
---

# How the platform understands your course

The Course Supporter platform does not read your course the way a student does. It turns the materials into text, builds section descriptions from that text, and then relies on exactly these descriptions when it reviews work automatically. Once you know which assistant reads what and when, it becomes clear what to upload as an educational material and what as a methodological one, and why a section description is worth reviewing before the first piece of work is submitted.

## Who reads what {#who-reads}

Every material in a course has a role: **educational** or **methodological**. Educational materials are what the student studies: lectures, notes, presentations, assignments. Methodological materials are your instructions on teaching and reviewing: what to require, what to pay attention to, model solutions, answer keys. You choose the role when you upload — see [Materials](materials.md#role).

| | Educational material | Methodological material | Section description |
|---|---|---|---|
| Student | sees it | never sees it | does not see it |
| Methodist | reads it in brief: the title, a short summary, the concepts; assignments — in full | reads it in full | writes it |
| Mentor | reads only the assignment the work was submitted for: its title and text | does not read it directly | reads it, but differently for different assignment types — [below](#summary) |

Two practical conclusions follow from this.

Requirements from your methodological guide reach the Mentor only through the section description, and for the "Short assignment (Коротке завдання)" and "Assignment (Завдання)" types — only through the "Description (Опис)" field of that description; what is written in the assignment itself, the Mentor reads directly. A guide the Methodist has not read, because you did not run it, will not be taken into account by the Mentor.

The student will not see a methodological material, but its content can show through in a review: the Mentor relies on the description written from it. If your guide contains a ready-made solution, check that the section description does not retell it.

## The three processing steps {#layers}

The platform prepares a course in three steps. On the screen they are called layers.

- **Layer 1 — turning into text.** Videos, presentations, texts, web pages and code become text that the platform's assistants work with.
- **Layer 2 — the structure of a material.** The text is divided into meaningful parts; each gets a title, a short summary and concepts. From them, a short summary of the whole material is put together.
- **Layer 3 — section descriptions by the Methodist.**

The platform goes through layers 1 and 2 by itself, right after a material is uploaded. The exception is materials with code: their processing starts when you confirm the roles of the files (the page "Materials with code" is in preparation). The "Done (Готово)" state next to a material means processing has finished.

Heavy processing — above all videos and large materials — is done when the order of jobs allows it and resources are free. At peak hours this can take up to a day; usually it is much faster.

You start layer 3 yourself: right-click the course card and choose "Generate description (Згенерувати опис)". Starting from the course card covers the whole course.

The menu item "Process materials (Обробити матеріали)" with the caption "Layer 1 + 2 (Шар 1 + 2)" is not a required step before the Methodist. It is needed only when the section has materials in the "Error (Помилка)" state: the item runs their processing again, and only in this section, without its subsections.

## The section description — what the Mentor works from {#summary}

The section description is written by the Methodist from the section's materials, and you can correct it. What the Mentor reads from the description depends on the assignment type. For the "Short assignment" and "Assignment" types, it is the section's "Description" field, plus the core concepts of the section and the course: the platform takes them when it puts together the list of criteria. The Mentor does not read the other fields for such assignments. For the "Project (Проєкт)" type, the Mentor reads five fields in every review: "Title (Заголовок)", "Description", "Learning objectives (Навчальні цілі)", "Success criteria (Критерії успіху)" and "Common mistakes (Типові помилки)", and from the course description — also the "Success criteria" of the whole course. The other fields are for you: they show how the Methodist understood the section, but they do not affect the review.

**Coming soon!** It is planned that the "Learning objectives", "Success criteria" and "Common mistakes" fields will become the basis of the list of criteria for the "Short assignment" and "Assignment" types as well, so requirements written in these fields will make it into the review.

For the "Project" type, the Mentor assesses the work not only against the course materials but also against the general practice of the field — without being tied to the course — and takes the student's previous work in this course into account. As of November 2026, the Mentor assesses work of the "Short assignment" and "Assignment" types against the list of criteria, as described below and on the [Assignments](tasks.md#review) page. What to write in the description fields and how to edit them — on the page [Section descriptions: the Methodist](methodist.md).

The Mentor reviews work against the list of criteria, and for the "Project" type also against the section description fields — and these sources follow different rules.

- **The assignment's list of criteria.** It is put together separately for each assignment — from the text of the assignment itself, the section's "Description" field and the core concepts of the section and the course — with the first submitted piece of work, and it is shared by all students. An edit of the "Description" field after that no longer reaches the list. More — [Assignments](tasks.md#criteria).
- **Section description fields — only for the "Project" type.** The Mentor reads the five fields named above anew in every review, so edits to them take effect from the next review — whether the description is approved or not. For the "Short assignment" and "Assignment" types, the description fields other than "Description" do not affect the review.

And one more rule worth knowing before you run the Methodist for the first time. A material or test added after the run marks the section description as outdated: the label "materials changed (матеріали змінились)" appears on the section card. Running the Methodist again rewrites only such sections, but in them it erases your edits and removes the approval. The change goes upwards: a material added to a lesson makes the descriptions of the lesson, the block above it and the whole course outdated.

Hence the two rules that [The author's path, step by step](index.md#steps) starts with: first all materials and tests, then the Methodist, then editing the descriptions; and get the "Description" field of a section with an assignment right before the first piece of work is submitted.
