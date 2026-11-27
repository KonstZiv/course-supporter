---
title: "Section descriptions: the Methodist"
language: en
role: authors
keywords:
  - authors
  - Methodist
  - section description
  - Generate description
  - Approve
  - running again
last_updated: 2026-11-27
---

# Section descriptions: the Methodist

The Methodist is an assistant program of the Course Supporter platform. It reads the materials of each section of the course and writes its description: what the section teaches, what the student has to show in their homework, which mistakes are the most common. The Mentor reads these descriptions when it reviews work, so it is worth running the Methodist at the right time, reviewing the descriptions and correcting them where needed. Which assistant reads what — on the page [How the platform understands your course](how-it-works.md#who-reads).

This page describes how everything works as of November 2026. The platform is developing, and names or messages on the screen may change.

## When and how to run it {#when}

Run the Methodist when all materials, assignments and tests have been uploaded to the course and all materials are in the "Done (Готово)" state. The Methodist silently skips materials that are still being processed or have an error, and when they finish processing, the section description becomes outdated. The Methodist does not read tests at all. The Methodist's work also uses units from your package ([Costs and history](costs-history.md#what-you-pay)), so do not run it too early: to take changes into account, you will have to run it again, and that costs units too. And run it before you add students: the Mentor reviews work submitted before the Methodist against the assignment text alone, without the requirements from your guide.

Right-click the course card and choose "Generate description (Згенерувати опис)" (below the item's name it says "Layer 3 (Шар 3)"). Starting from the course card covers the whole course; starting from a section card — that section and everything below it.

Before starting, the app opens the "Run the Methodist? (Запустити Методиста?)" window. It says what exactly the Methodist will describe — the chosen section and all its subsections whose description is out of date or not written yet — and roughly how much it will cost: "Estimated cost: about …" («Орієнтовна вартість: приблизно …»). Press "Run (Запустити)" to start, or "Cancel (Скасувати)". If all section descriptions here are already up to date, the window says so: "the Methodist has nothing to write, and no units are used" («Методистові нічого складати, юніти не списуються»). If you do not have enough units, the window shows how many you are short, and the "Run" button stays unavailable until you buy a package ([Costs and history](costs-history.md)).

If units run out while the Methodist is already working, the run card shows "The section descriptions were not written: there were not enough units. After buying units, run the Methodist again." («Описи розділів не складено: на це не вистачило юнітів. Після купівлі юнітів запустіть Методиста ще раз.»)

The order is simple: all materials and tests — before the first run of the Methodist, editing the descriptions — after it. Why it is so is explained in the section [Running the Methodist again](#rerun).

## What happens during generation {#progress}

At the bottom right, a "Generating description · "…"" («Генерація опису · «…»») card appears (instead of the three dots — the name of the course or section) with the "Processing (Обробляється)" state and a "node N of M" («вузол N з M») counter. The Methodist goes through the covered part of the course twice — first from the deepest sections up to the card you started it from, then from that card down — so M is twice the number of cards the run covers: the card you started it from and all the cards below it. For the first few minutes the counter may stay at zero: this is normal.

Generation takes a long time: for a large course — tens of minutes. If the platform is busy with other heavy processing at that time, generation starts when its turn comes.

While generation is running, do not close or refresh the course page: the card will disappear. Generation itself does not stop because of this. That the Methodist is still working, you can see at the top, in the "Recent jobs (Останні роботи)" strip: a "Processing" row with the name of the course or section you started it from. In a narrow window, the strip is collapsed to an icon — press it. Do not start the Methodist a second time.

When the card shows "Done", refresh the page: "Draft (Чернетка)" labels appear on the section cards.

If the card shows an error for a section, the error text may be in English and technical. Copy it word for word and send it to the platform administrator — [how to do it](troubleshooting.md#contact).

## If the run is rejected {#rejected}

Instead of the generation card, a "Rejected (Відхилено)" card may appear with the text "There are outdated nodes outside the subtree (…). Go to them to review, or generate anyway with a forced run." («Поза піддеревом є застарілі вузли (…). Перейдіть до них, щоб переглянути, або згенеруйте все одно з примусовим прогоном.») (in brackets — the number of such sections) and the "Generate with force (Згенерувати з force)" button.

The idea of this check is to warn you that the course has sections with outdated descriptions that your run does not cover. But as of November 2026, the check also counts sections of other courses — all those your access key opens. So, if you have several courses, you may see the refusal even when everything is fine with this course. If the list on the card shows short strings of letters and digits instead of names, these are sections of other courses.

If you are running the Methodist from the course card, press "Generate with force": descriptions will be generated only for this course.

## What a section description contains {#fields}

Click the section card and, in the panel, press "View/edit description (Переглянути/редагувати опис)". The button appears when a description already exists; before the first run of the Methodist, it is not there. The "Node description — … (Опис вузла — …)" window opens, with the section title.

| Field on the screen | What is in it | Does the Mentor read it | Can you edit it |
|---|---|---|---|
| "Title (Заголовок)" | a short name for what the section teaches | no | yes |
| "Description (Опис)" | one paragraph: what the section covers and what the student does in it | goes into the list of criteria of all assignments | yes |
| "Learning objectives (Навчальні цілі)" | what the student will be able to do after the section | no | yes |
| "Knowledge (will know) (Знання (буде знати))" | facts, concepts, principles | no | yes |
| "Skills (will be able to) (Навички (буде вміти))" | what the student can do in practice | no | yes |
| "Success criteria (Критерії успіху)" | what the student has to show in the submitted work | no | yes |
| "Assessment approach (Підхід до оцінювання)" | how to assess: what to send back for rework, what to accept with a reminder, what not to count as a mistake | not yet | yes |
| "Teaching approach (Підхід до викладання)" | how the section's materials teach | no | yes |
| "Key activities (Ключові активності)" | what the student does in the section | no | yes |
| "Common mistakes (Типові помилки)" | mistakes in the work — by signs visible in the work itself | no | yes |
| "Core concepts (Основні концепти)", "Secondary concepts (Другорядні концепти)" | concepts gathered from the section's materials | no; but the core ones go into the list of criteria | no |
| "Enclosing context (Охоплюючий контекст)" | the section's place in the course | no | no |
| "Methodist's observations (Спостереження методиста)" | the Methodist's notes for you | no | no |

The words "Core concepts" and "Secondary concepts" on the screen mean the same as "concepts" on this page.

**Coming soon!** It is planned that the "Learning objectives", "Success criteria" and "Common mistakes" fields will become the basis of the list of criteria, so requirements written in these fields will make it into the review.

At the bottom of the window is the line "Documents: … (with subtree …) · characters: … (with subtree …)" («Документів: … (з піддеревом …) · символів: … (з піддеревом …)»): how many materials and characters of text the section has by itself and together with everything below it. The numbers are counted at the time of generation: only processed materials, without tests.

Descriptions come in two kinds.

- **A section with assignments** (for example, a lesson). "Success criteria" and "Common mistakes" here are about the work submitted in this very section.
- **A section without assignments** (a course, a block, an introduction). "Success criteria" are the outcomes at the end: *after finishing the block, the student …*. "Common mistakes" are always empty.

The platform considers a section to have assignments if it contains a processed material with an assignment type. A lesson that has only a test created in the editor will be described by the Methodist as a section without assignments.

## Where to start reviewing {#priority}

There are many descriptions; check them in this order.

1. **Sections with assignments.** The Mentor reads their "Description" field — it goes into the list of criteria of every assignment. See whether these really are your requirements, whether there are requirements from other lessons, whether each point can be checked from the submitted work.
2. **The course description** (the course card). Only the "Core concepts" field is taken from the course description. Here you should have the outcomes of the whole course, not the requirements of a single lesson.
3. **"Methodist's observations"** in each section with an assignment: where the Methodist formulated requirements itself and what it did not carry over from your guide.
4. **Intermediate sections** (blocks). The Mentor does not read their descriptions; edit them if you like.

## "Methodist's observations" {#observations}

The description of each section has a "Methodist's observations" field — these are the Methodist's notes for you. The Mentor does not read them, so they do not affect reviews. Their use is to point out where to check the description more carefully.

- **How many methodological materials the section had.** If none, the Methodist formulated the requirements itself, from the educational materials and assignments — check such a description especially carefully.
- **Which fields the Methodist formulated itself,** because your guide says nothing about them — check them first.
- **What was deliberately not carried over from the guide** — requirements that make the Mentor guess how the student worked, without a visible sign in the submitted work. If such a requirement is important, state it in terms of what can be seen in the work.
- **Materials that did not fit.** The Methodist reads the section's methodological materials and assignments in full as long as they fit within the limit of 200,000 characters per section (an ordinary lesson is 10–40 thousand). A material that did not fit, it sees only in brief and names it in the observations; a shorter material after it may still be read in full. In that case, shorten the material or move some of the materials into a subsection.

The observations are written tersely and in places with technical words. For example, the phrase *Жодний методичний документ чи завдання не надано лише як скорочений виклад через вичерпання бюджету повного тексту* (literally: "No methodological document or assignment is provided only as a shortened summary due to exhaustion of the full-text budget") simply means: everything was read in full. There are plans to make the wording of the observations clearer.

## How to edit and approve {#edit}

At the top of the description window is the state — "Draft — not approved (Чернетка — не затверджено)" or "Approved (Затверджено)" — and three buttons.

- **"Edit (Редагувати)"** turns on "Edit mode (Режим редагування)". In lists, each point is a separate row; "Add (Додати)" adds a point, the bin icon removes it. In the "Knowledge (will know)" and "Skills (will be able to)" fields, a point has two parts — "Name (Назва)" and "Description (Опис)", and the button is called "Add pair (Додати пару)". When you are done, press "Save (Зберегти)"; "Cancel (Скасувати)" puts everything back as it was.
- **"Approve (Затвердити)"** marks that you have checked the description. The state becomes "Approved", and the same label appears on the section card. Your edits stay.
- **"Accept generated (Прийняти згенероване)"** puts the Methodist's text back in place of your edits and approves it straight away.

Important: "Accept generated" does not ask again and cannot be undone. All your edits of this description will be lost. Press this button only when you want to give up your own edits.

Approval is a label for you, so that you do not get lost among the descriptions. The Mentor reads a description the same way whether it is approved or not. A saved edit of the "Description" field reaches the review only while the assignment's list of criteria has not been put together yet (see [below](#timing)).

After pressing "Approve" or "Accept generated", check that the top of the window says "Approved". If the app said nothing and the state did not change, press the button again or refresh the page.

The "Compare with previous version (Порівняти з попередньою версією)" button appears if the Methodist rewrote the description during a repeated generation. It marks with the word "changed" («змінено») the fields that differ from the previous version. The app does not show the previous text itself, and it cannot be restored. After "Approve", the previous version disappears for good: the compare button will no longer be there once you open the description window again.

## When an edit of the description reaches the review {#timing}

The Mentor reviews work against the assignment's list of criteria — for assignments of all types, including a project. The list is put together separately for each assignment — from the text of the assignment itself, the section's "Description" field and the core concepts of the section and the course — with the first submitted piece of work, and it is shared by all students. An edit of the "Description" field after that no longer reaches the list. More — [Assignments](tasks.md#criteria). The other description fields do not affect the review.

The practical conclusion: get the "Description" field of a section with an assignment right before students — or you yourself, on behalf of a test student — start submitting work.

## Running the Methodist again {#rerun}

When it runs again, the Methodist rewrites the description only of the sections in which something changed: a material was added, deleted or processed again, a test was added or hidden, a subsection was added or deleted — in the section itself or in any section below it. It skips the other sections — even if the run was "with force".

In a rewritten section:

- your edits disappear — in their place is the Methodist's new text;
- the approval is removed, and the description is a "Draft" again;
- the previous version remains only for comparison; it cannot be restored.

If students have already submitted work for an assignment of this section, the new description does not reach the review by itself: the assignment's list of criteria has already been put together. It is updated when the assignment itself changes; otherwise, correct the list on the "Criteria (Критерії)" screen ([Assignments](tasks.md#criteria)).

The change goes upwards. If you added a material to a lesson, the descriptions of the lesson, the block above it and the whole course become outdated — and all three will be rewritten. Your edits in the lesson description do not get into the descriptions of the block and the course: those are written from what the Methodist wrote.

Which descriptions are outdated is shown by the "materials changed (матеріали змінились)" label on the section card. To see it after changes, refresh the page.

What the Methodist will not notice: a change of a material's role (educational ↔ methodological) and of an assignment type. The description is not marked as outdated because of this, and running it again will skip it. If the description needs updating after such a change, contact the platform administrator and name the section ([how to do it](troubleshooting.md#contact)); to update the descriptions of the block and the course above it as well, ask for an update from the course. Your edits in the rewritten descriptions will be lost.

The Methodist does not read tests. But a new test, like any new material, marks the section description as "materials changed". There is no need to rerun the Methodist because of a test: the description will not get better, and your edits will be lost. Editing and republishing a test does not affect descriptions; hiding a test does, like deleting a material.

Before running it again, copy your edits into a separate file: after the run, you will not be able to get them back.

Hence the rule: all materials and tests — before the first run of the Methodist; editing the descriptions — after it.
