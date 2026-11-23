---
title: Materials
language: en
role: authors
keywords:
  - authors
  - materials
  - upload
  - educational material
  - methodological material
  - processing states
  - upload errors
last_updated: 2026-11-23
---

# Materials

A material is a file or a link that you put into a section of the course: lecture notes, a presentation, a video, a methodological guide, an assignment brief. The Course Supporter platform turns each material into text and works out its structure; its assistants — the Methodist and the Mentor — then work on this text ([How the platform understands your course](how-it-works.md)).

On the screen, a material is sometimes called something else — for example, in the "Document type (Тип документа)" window. It is the same thing.

## What you can upload {#formats}

| What | Formats | Limits |
|---|---|---|
| Video as a file | mp4, mov, avi, mkv, webm | up to 1 GB; up to 150 minutes |
| Video by link | a link to YouTube or Vimeo, or a direct link to an mp4, mov, avi, mkv, webm file | up to 150 minutes |
| Audio | mp3, wav, m4a, ogg, flac | up to 100 MB; up to 150 minutes |
| Presentation | pdf, pptx, ppt | up to 50 MB; up to 100 slides or pages |
| Text | md, txt, docx, html | up to 100 MB |
| Web page | any other link | — |
| Code *(for programming courses)* | separate code files; a zip archive | archive — up to 100 MB, up to 200 MB unpacked, up to 1,000 files; the platform does not open archives inside an archive |
| Test | a yaml file | up to 256 KB |

The platform processes a PDF as a presentation — page by page. Split a PDF longer than 100 pages into parts. Pages without text — scans, diagrams, screenshots — the platform describes by itself from the image, so such presentations and PDFs are processed too. Rarely, a page cannot be described — then the material gets the "Error (Помилка)" state (see [If a material is not accepted or not processed](#problems)).

Split a video or audio longer than 150 minutes into parts as well.

Links to videos on other sites, apart from YouTube and Vimeo, the platform processes as an ordinary web page: it reads the text of the page, not the video itself. Upload such a video as a file.

The app checks some of the limits before sending and tells you straight away if a file is too large or a video is too long.

You can also create a test without a file, right in the app — see [Tests](tests.md). Materials with code (archives and separate files) have their own rules — the page "Materials with code" is in preparation.

## How to upload {#upload}

There are three ways.

- **Drag files.** Click the section card — the section panel opens. Drag files into the "Drag files here (Перетягніть файли сюди)" zone.
- **From the menu.** Right-click the section card → "Upload material (Завантажити матеріал)".
- **By link.** In the section panel, paste the address into the "https://youtu.be/... або URL" field ("https://youtu.be/... or URL") and press "Add (Додати)".

Before uploading, the app opens the "Document type" window — more on it below. While files are being sent, you can see the progress in place of the drop zone in the section panel (or in the menu, if you uploaded through it): "Sending file {N} of {N}" («Надсилаємо файл {N} з {N}»), then "The server is receiving the file…" («Сервер приймає файл…»).

<!-- frame 9: the section panel while sending — "Sending file 1 of 3 (Надсилаємо файл 1 з 3)" shown in place of the drop zone -->

Each file becomes a separate material.

The choice in the "Document type" window applies to all files you upload together. So upload in batches: educational materials separately, methodological ones separately, assignments separately.

The order of materials in a section is the order of uploading: the student sees them in the sequence you uploaded them in, and a new material always becomes the last. You cannot change the order later. Upload in the sequence the student should go through the materials: overview, notes, assignment. To "move" a material to the end, you will have to delete it and upload it again — and units will be charged again for the new processing (see [Who pays for processing](#cost)). As of October 2026, there is no other way.

## The "Document type" window: the material's role {#role}

Every material has a role, and it is the first thing you choose in the window. Until a role is chosen, the "Upload (Завантажити)" button is inactive.

- **"Educational (Навчальний)"** — a material for the student. The student will see it in the course as soon as the material is processed.
- **"Methodological (Методичний)"** — a material for the platform's assistants. The student will never see it. Put review guides, model solutions, answer keys to assignments, lesson scripts here. This is the only way to set review requirements so that the student does not see them — more [below](#methodical).

The captions next to the roles in the window itself — "Conveys information to the student" («Доносить інформацію студенту») and "Declares the course's intentions" («Декларує наміри курсу») — do not say who will see the material. So remember: **the student sees educational materials, not methodological ones.** A material with the "Test (Тест)" type is always educational: the app says so itself.

Below the roles, the window asks whether the material is a specific assignment. If not, leave "Not an assignment (Не завдання)". About assignment types — on the [Assignments](tasks.md) page.

<!-- frame 8: the "Document type (Тип документа)" window — "Educational (Навчальний)" and "Not an assignment (Не завдання)" chosen -->

For example — how one lesson of the course "Agentic development in Python" is laid out:

| File | What it is | Role |
|---|---|---|
| lecture notes | educational text | educational |
| presentation | lecture slides | educational |
| basic-level assignment, advanced-level assignment | homework assignments | educational, "Assignment (Завдання)" type |
| lesson guide, review steps for each assignment | instructions for review | methodological |
| model solution | a sample for review | methodological |
| lecture and demo script | context for the Methodist | methodological |

What to write in methodological materials and what not to do — [below](#methodical); how they reach the reviews — in the section [Who reads what](how-it-works.md#who-reads).

You can change the role after uploading, too: in the material's row in the section panel, click the "📚 навчальний" (educational) or "📋 методичний" (methodological) label — the tooltip next to it says "Click to change the type" («Натисніть щоб змінити тип»).

<!-- frame 10: a material row close up — title, state, the "📚 навчальний" label, the "Retry processing (Повторити обробку)" or "Restart processing (force) (Перезапустити обробку (force))" and "Delete (Видалити)" icons on hover -->

Important: if the Methodist has already written the section description, changing the role does not update it. The "materials changed (матеріали змінились)" label will not appear, and running the Methodist again will skip this section. So it is better to choose the role correctly right away. If you changed it after the Methodist had run, write to the Course Supporter platform administrator ([how to contact them](../index.md#authors)) and name the section: they can make the Methodist read the section again. Your edits in the rewritten descriptions will be lost. This is how the platform works as of October 2026.

### Methodological material: your review requirements {#methodical}

The student reads the assignment text but not the methodological guide. So the methodological material is the place to write what you expect from the work, what to pay attention to, which mistakes are typical and what should not be counted as a mistake. These requirements reach the review in two steps: the Methodist reads the guide and carries the requirements over into the section description, and the Mentor reads the "Description (Опис)" field from that description — once, when the list of criteria is put together ([Who reads what](how-it-works.md#who-reads)). So the guide affects the emphasis of assessment only when the Methodist has read it and you have reviewed the description.

What to do:

- Put the guide in the same section as the assignment, with the "Methodological" role.
- State requirements in terms of what can be seen in the submitted work: *the archive contains a report file*, *the script takes a file path*.
- Upload the guide before running the Methodist, and then review the section description: the most important requirements for assignments of all types, including a project, should be in the assignment text and in the "Description" field.

**Coming soon!** It is planned that the "Learning objectives (Навчальні цілі)", "Success criteria" and "Common mistakes" fields will become the basis of the list of criteria, so requirements written in these fields will make it into the review.

What not to do:

- **Do not put the guide in a different section from the assignment.** The Methodist reads full text only from the materials of the section itself, and from subsections it takes only brief summaries.
- **Do not write requirements that cannot be seen in the submitted work** — *the student had to work it out alone*, *worked without hints*. The Methodist will not carry such a requirement over into the description and will say so in the "Methodist's observations (Спостереження методиста)" field.
- **Do not expect separate requirements for each assignment of a section.** A section has one description, and all its assignments — say, basic and advanced level — share it. So in the guide, say which assignment a requirement applies to.
- **Do not leave allowances only in the "Assessment approach (Підхід до оцінювання)" field.** The Mentor does not read this field yet and may send back work that the guide says to accept. Add the most important things — *this is not a mistake*, *accept with a reminder* — to the assignment text or to the section's "Description".
- **Do not assume that a model solution in the guide will stay secret.** The Mentor does not read the guide, but the description is written from it, so the content of the solution may get into the description and show through in a review. Review the description.
- **Do not make the guide too large.** The Methodist reads the section's methodological materials and assignments in full up to 200,000 characters (an ordinary lesson is 10–40 thousand); whatever does not fit, it sees only in brief.

If a section has no guide, the Methodist will formulate the requirements itself — from the assignment text and the educational materials. Check such a description especially carefully. How to edit the description fields — on the page [Section descriptions: the Methodist](methodist.md).

## Material states {#states}

Next to each material in the section panel, you can see its state.

| State | What it means | What to do |
|---|---|---|
| "Queued (У черзі)" | the material is waiting its turn for processing | wait |
| "Processing (Обробляється)" | the platform is turning the material into text and working out its structure | wait; below the row you can see the progress of the current step or how long processing has been running |
| "Awaiting confirmation (Очікує підтвердження)" | happens only with materials with code — both an archive and a separate file: the roles of the files have to be confirmed | press "Confirm roles (Підтвердити ролі)" (the page "Materials with code" is in preparation) |
| "Done (Готово)" | the material is processed; an educational one is already visible to students | nothing |
| "Error (Помилка)" | processing failed | read the message and act according to the table below |

<!-- frame 11: a section with materials in different states — "Queued (У черзі)", "Processing (Обробляється)" with duration, "Done (Готово)" -->

Heavy processing — above all videos and large materials — is done when the order of jobs allows it and resources are free. At peak hours this can take up to a day; usually it is much faster. You can close the page: processing continues without you.

What is being processed right now, you can see in the "Recent jobs (Останні роботи)" strip in the app header. What happened to your materials before — on the "Materials history (Історія матеріалів)" page.

<!-- frame 14: the "Materials history (Історія матеріалів)" page with several rows -->

## If a material is not accepted or not processed {#problems}

A refusal comes in one of two places: during upload — the app does not accept the file, or after it — the material gets the "Error" state. The table shows what the most common messages mean.

| What you see | What happened | What to do |
|---|---|---|
| "This file type is not supported yet." («Такий тип файлу поки не підтримується.») | the format is not in the list | save the material in one of the supported formats |
| "The file is too large. Make it smaller or split it into parts." («Файл завеликий. Зменште його або розділіть на частини.») | the size limit is exceeded | split the file |
| "The presentation has too many slides. Split it into parts and upload each one separately." («У презентації забагато слайдів. Розділіть її на частини й завантажте кожну окремо.») | more than 100 slides or pages | split the presentation |
| "Invisible service characters were found in the text…" («У тексті знайдено невидимі службові символи…») | hidden characters got into the text, usually when copying from a web page | save the file again in an ordinary text editor |
| "The file is not saved in UTF-8 encoding…" («Файл збережено не в кодуванні UTF-8…») | the platform could not read the letters | open the file in an editor and save it as UTF-8 |
| "The content of the file does not match its extension…" («Вміст файлу не збігається з його розширенням…») | the file is named after a format it is not really in | save the file in the right format |
| "Could not open the link…" («Не вдалося відкрити посилання…») | the source is not available | check the link; the video may be private or deleted |
| "Could not read the archive…" («Не вдалося прочитати архів…»), "The archive is too large when unpacked…" («Архів завеликий у розпакованому вигляді…»), "The archive contains symbolic links…" («Архів містить символьні посилання…») | the archive is damaged, too large or contains something the platform does not accept | create the archive again |
| a message saying the video is too long (longer than 150 minutes) | the video by link exceeds the limit | split the video into parts |
| "The document has no extractable content — no text block or code file to process was found." («Документ не містить видобувного вмісту — не знайдено жодного текстового блоку чи файлу коду для обробки.») | the file has no text the platform can read | check that the file is not empty and that the text in it really is text, not an image |
| "Could not process this material — try again. If it happens again, write to us and we will sort it out." («Не вдалося обробити цей матеріал — спробуйте ще раз. Якщо повториться, напишіть нам, ми розберемося.») | a failure during processing; it can be on the platform's side or in the file | press "Retry processing (Повторити обробку)" (see below); if the error has repeated twice, write to the Course Supporter platform administrator |

The buttons in a material's row are icons: they appear when you hover over the row, and the icon's name is shown in the tooltip. "Retry processing" is the icon with a circular arrow; a material in the "Error" state has it. A material in the "Done" state has "Restart processing (force) (Перезапустити обробку (force))" — the word force here means "anyway": process it again and replace the current result. The third icon is "Delete (Видалити)".

Important: repeated processing goes through all the steps from the beginning, so units are charged for it again, just as for the first time (see [Who pays for processing](#cost)). Do not repeat it more than twice in a row: if the error has not gone away, look for the cause together with the platform administrator rather than by retrying.

<!-- frame 12: a material in the "Error (Помилка)" state with the message text -->

### The material did not pass the security check {#safety}

This is a separate case, because it looks different depending on when it happens and does not name the cause.

- **During upload**, the app says: "The material did not pass the content security check. If you are sure the material is fine, write to us and we will check." («Матеріал не пройшов перевірку безпеки вмісту. Якщо ви впевнені, що з матеріалом усе гаразд, напишіть нам — ми перевіримо.») This is how the platform checks text files (md, txt, html): it looks for phrases that resemble instructions to the model — for example, a demand to ignore previous instructions.
- **After upload**, the material is accepted at first and later disappears from the section. This is how other formats are checked: docx, pdf, presentations, links and code — they are checked during processing. The "Materials history" page then shows "Material deleted" («Матеріал видалено»), and below it a caption saying the author did it, with the date, although it was not you who deleted the material but the check. As of October 2026, the app does not tell the two apart. Besides instruction-like phrases, at this step a material can also be rejected by the model if it considers it unrelated to the course or harmful.

The platform deliberately does not say which cause it found. Courses about artificial intelligence and security quote such phrases quite legitimately — and get refused too.

What to do: find quotations of phrases that resemble instructions to the model in the material and retell them in your own words, or break the quotation into parts. Upload the material again. If you cannot find the cause, write to the Course Supporter platform administrator: they can see why the material was rejected.

<!-- frame 13: a refusal during upload — "The material did not pass the content security check… (Матеріал не пройшов перевірку безпеки вмісту…)" -->

## Replace, delete, upload again {#replace}

You cannot replace the file in a material that is already uploaded. To update a material, delete the old one — hover over the row and press the bin icon, "Delete", — and upload the new one.

The platform does not recognise repeats: the same file uploaded twice will be processed twice, units will be charged twice, and two identical materials will appear in the section. Do not upload a file a second time just in case.

Deleting a material, like adding a new one, changes the section: if the Methodist has already written its description, the "materials changed" label appears on the section card. The same label appears on the sections above it. To see it, refresh the page. What this means for the Methodist — in the section [The section description](how-it-works.md#summary).

## Who pays for processing {#cost}

Material processing is a paid service of the Course Supporter platform: the materials are read and analysed by models, and that costs money. The author pays for processing the materials of their course — with units from their package, just as a student pays for the review of their work.

If the platform repeated processing on its own initiative, no units are charged from your package for it. If processing was repeated because of your changes — you deleted a material and uploaded a new one, or pressed "Restart processing (force)" yourself — units are charged as for ordinary processing.

What happens when your available units are not enough, and where your costs are shown — on the page [Costs and history](costs-history.md).

Next — [Assignments](tasks.md).
