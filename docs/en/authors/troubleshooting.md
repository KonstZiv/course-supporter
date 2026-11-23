---
title: If something goes wrong
language: en
role: authors
keywords:
  - authors
  - errors
  - limitations
  - what to do
  - platform administrator
last_updated: 2026-11-24
---

# If something goes wrong

The Course Supporter platform is developing actively. Because of this, it has temporary limitations, and we and our users sometimes find defects. We are grateful to everyone who writes to us about a defect they found or suggests how to make the platform better, and we try to fix what is found as quickly as possible.

Below is what is worth knowing already, and what to do in each case. Find in the tables what you see on the screen: the next column says what to do, and the link leads to the page where it is described in more detail. How to write to us — [at the end of the page](#contact).

This is how everything works as of November 2026. Some of the limitations will be fixed — then they will disappear from here.

## Signing in and courses {#login}

| What you see | What to do | More |
|---|---|---|
| The "My courses (Мої курси)" page shows "No courses yet (Ще немає курсів)", and other pages show "API error 401" | Your courses have not gone anywhere: the access key stopped working. Press "Sign out (Вийти)" and sign in again; if that does not help, write to the platform administrator | [Signing in](first-course.md#login) |
| "Invalid API key. Check it and try again." («Невірний API ключ. Перевірте та спробуйте ще раз.») | Check that the key was copied in full. If it was, the key may have been revoked or may have expired — write to the platform administrator | [Signing in](first-course.md#login) |
| A section was created in the wrong place, and a card you moved with the mouse returned to its place after the page was refreshed | The order of sections cannot be changed in the app. Delete an empty section and create it again in the right sequence; if the section is already filled, the platform administrator can fix the order | [Order of sections](structure.md#order) |
| You changed the course language, but materials, section descriptions and tests stayed in the previous language | The platform only recorded the new language and translated nothing. Do not choose "Auto-detect (Автовизначення)" in the language list. If the language has to be changed in a filled course, write to the platform administrator | [Course language](first-course.md#language) |

## Materials {#materials}

| What you see | What to do | More |
|---|---|---|
| The app did not accept a file, or a material got the "Error (Помилка)" state | Find the message in the table and act according to it | [If a material is not accepted or not processed](materials.md#problems) |
| "The material did not pass the content security check…" («Матеріал не пройшов перевірку безпеки вмісту…») — or a material was accepted at first, then disappeared from the section, and the "Materials history (Історія матеріалів)" page shows "Material deleted (Матеріал видалено)" with a caption saying the author did it | This is the security check, not your action; the platform does not name the cause. Find quotations of phrases that resemble instructions to the model in the material and retell them in your own words; if you cannot, write to the platform administrator | [The material did not pass the security check](materials.md#safety) |
| "Could not process this material — try again. If it happens again, write to us and we will sort it out." («Не вдалося обробити цей матеріал — спробуйте ще раз. Якщо повториться, напишіть нам, ми розберемося.») | Hover over the material's row and press the "Retry processing (Повторити обробку)" icon — no more than twice: units are charged for processing that finished. If that does not help, write to the platform administrator | [If a material is not accepted or not processed](materials.md#problems) |
| "Processing has not started: there were not enough units for it…" | This is not a failure of the material: your package does not have enough units. Press "Buy units (Купити юніти)" under the message (or open "Units (Юніти)"), buy a package and press "Retry processing (Повторити обробку)" | [Who pays for processing](materials.md#cost) |
| You chose "Process materials (Обробити матеріали)", and the window "There are no materials with an error in this section…" («Матеріалів із помилкою в цьому розділі немає…») appeared | This is how it should be: the item repeats processing only for materials in the "Error" state and is not needed before the Methodist | [The section menu](structure.md#menu) |
| A material has the wrong role — educational instead of methodological or the other way round | Click the "📚 навчальний" (educational) or "📋 методичний" (methodological) label in the material's row. If the Methodist has already written the section description, it will not notice this change: write to the platform administrator and name the section | [The material's role](materials.md#role) |
| A material got the wrong assignment type, and the type is not shown in the row | The assignment type is not shown after uploading and cannot be changed in the app. Delete the material and upload it again with the right type (units will be charged again for processing), or write to the platform administrator: they can change the type without processing it again | [Worth knowing before uploading](tasks.md#before-upload) |
| Materials are in the wrong order, or a file in an already uploaded material needs replacing | The order is the order of uploading, and you cannot replace the file in a material. Delete the material and upload a new one. Do not upload the same file twice "just in case": it will be processed and charged twice | [How to upload](materials.md#upload), [Replace, delete, upload again](materials.md#replace) |

## Materials with code *(for programming courses)* {#code}

The page "Materials with code" — archives and separate code files — is in preparation. For now, here are the cases in which advice is needed most often.

| What you see | What to do | More |
|---|---|---|
| A material with code is in the "Awaiting confirmation (Очікує підтвердження)" state and does not move on | This is not a failure: the material is waiting for you to confirm the roles of the files. Press "Confirm roles (Підтвердити ролі)", review the roles and press "Confirm and process (Підтвердити й обробити)". Do not leave the material in this state: as of November 2026, the student already sees an educational material that is "Awaiting confirmation". Roles are set once: after processing, they can be changed only by deleting the material and uploading it again | [Material states](materials.md#states) |
| On the "Material file roles (Ролі файлів матеріалу)" page, you are asked to sort out the roles, and it is not clear what they do | The full explanation is shown in a window only on the first visit. You can open it again with the "What is this for? (Навіщо це?)" link. If you just press "Confirm and process", the project's own files keep the "Full (Повний)" role: then the Mentor's attention gets spread thin and processing costs more. It is worth leaving "Full" only for files important to the lesson's topic | — |
| In the row of a processed archive, a yellow block "… files not read" («… файлів не прочитано») | Almost always everything is fine: this number includes service files, images, libraries and files you yourself marked "Not essential for the topic (Несуттєвий для теми)". Expand the block and look for only two causes: "Encoding not recognised — the text could not be read." («Кодування не розпізнано — текст прочитати не вдалося.») and "The content of the file does not match its extension." («Вміст файлу не відповідає його розширенню.»). Do not upload the archive again without need | — |
| A project's starter archive could not be prepared; below the "Error" label is a technical line in English | Check that it is a `.zip`, `.tar.gz`, `.tgz` or `.gz` archive, that it is not damaged and that it is no larger than 200 MB unpacked; create the archive again and press "Again (Повторно)". If that does not help, send this line to the platform administrator | [The project and the starter archive](tasks.md#project) |

## Tests {#tests}

About the test editor — the [Tests](tests.md) page.

| What you see | What to do | More |
|---|---|---|
| "Could not save the draft. Check your connection and try again." («Не вдалося зберегти чернетку. Перевірте з'єднання й спробуйте ще раз.») (the same after other actions: "Could not publish the test." («Не вдалося опублікувати тест.») and so on) | The app gives this advice also when the server did not name a cause — even if your internet is fine. Do not refresh or close the page: unsaved edits live only in this tab. Try saving again in a minute. If it does not work, copy your texts into a separate file and write to the platform administrator | [Save the draft](tests.md#editor-save) |
| Instead of editing fields — the test as continuous text, and you cannot edit it | You are in "As the student sees it (Як бачить студент)" mode. Switch back to "Edit (Правка)" | [As the student sees it](tests.md#editor-student) |
| You pressed "Hide test (Приховати тест)", and the test is needed again | Hiding cannot be undone: you cannot bring the test back in the author app. To remove questions temporarily, edit the test and publish a new version. Hide only a test that is no longer needed | [YAML and hiding](tests.md#editor-yaml) |

## The Methodist {#methodist}

How to work with section descriptions — on the page [Section descriptions: the Methodist](methodist.md). Below is what confuses people most often.

| What you see | What to do | More |
|---|---|---|
| You refreshed or closed the course page during generation, and the "Generating description (Генерація опису)" card disappeared | Generation goes on without the card. So keep the course page open until generation ends. Do not start the Methodist a second time. "Draft (Чернетка)" labels on section cards do not mean it has finished: they appear already during the first pass | — |
| A "Rejected (Відхилено)" card with a list of sections and the "Generate with force (Згенерувати з force)" button | This refusal can be false: the check takes into account sections of all courses your access key opens, not only of the one where you run the Methodist. If you run it from the course card, press "Generate with force" — descriptions will be generated only for this course | — |
| The counter in the generation card stays at zero for a long time, and the numbers in it are twice the number of sections | This is not a failure: the Methodist goes through the course twice. Wait — the counter will move as soon as the first description is ready | — |
| The card showed "Done (Готово)", but nothing changed on the section cards | Refresh the page — "Draft" labels will appear. Do not start the Methodist a second time | — |
| You pressed "Approve (Затвердити)" or "Accept generated (Прийняти згенероване)", but the state did not change and there is no message | After pressing, check whether the top of the window says "Approved (Затверджено)". If not, press again or refresh the page | — |
| After "Accept generated", your edits of the description disappeared | This button puts the Methodist's text back in place of all your edits of this description and approves it — without asking, and it cannot be undone. To keep your edits, press "Approve". Make the important edits again | — |
| After running the Methodist again, some descriptions are "Draft" again, and your edits are not in them | The Methodist rewrites the descriptions of sections whose materials or tests changed, and of all sections above them — together with your edits. So first upload all materials and tests, then run the Methodist, then edit the descriptions. Before running it again, copy your edits into a separate file: "Compare with previous version (Порівняти з попередньою версією)" only marks the changed fields and does not show the previous text, and after "Approve" the previous version disappears for good | [The section description](how-it-works.md#summary) |

## General {#general}

| What you see | What to do | More |
|---|---|---|
| You have paid, but there are no units | The "Units (Юніти)" page checks the payment by itself: first "Adding… (Зараховуємо…)", then "The payment has not arrived yet… (Оплата ще не надійшла…)" — you can close the page, the units appear within a few minutes; press "Check again (Перевірити ще раз)". If there are still no units after an hour, write to the platform administrator | [Costs and history](costs-history.md#package) |
| Under the header — "Reviews of your students' work are waiting for units: … To start them, you need … more" | You pay for these reviews, and your package does not have enough units. Press "Buy units (Купити юніти)": after the purchase the reviews start by themselves, nobody has to send the work again | [Costs and history](costs-history.md) |
| You clicked, and nothing happened: this happens, in particular, with changing a material's role, with "Add subsection (Додати підрозділ)", with "Delete (Видалити)" in the section menu, with creating a course | In these places, the app may silently not perform the action. Refresh the page and check whether the action happened. If not, repeat it. If it keeps happening, write to the platform administrator | — |
| English and technical words here and there on the screen | Language names in the list are in English, for example "Ukrainian"; tooltips and buttons contain "force", "STT", "LLM", "external ID". Their meaning is explained by the page "Guide to screens and names" (in preparation) | — |

## How to write to the platform administrator {#contact}

If the guide advises you to contact the platform administrator, or the app says "write to us" («напишіть нам»), write to support@pythoncourse.me.

### The button in the app {#contact-button}

Below messages that advise "write to us", and on the "Help (Довідка)" page, there is a "Write to the platform administrator (Написати адміністраторові платформи)" button. Press it — a ready-made email opens. The address and subject are already filled in, and the text contains the message you saw, the name of the course, section, material or test, the page address and the time. Add what you were doing before that, and send it.

After you press it, the address and the buttons "Copy address (Скопіювати адресу)" and "Copy email text (Скопіювати текст листа)" appear below the button: you need them if your mail program did not open — then write the email yourself, pasting the copied text. On the "Help" page, the address and these buttons are visible straight away.

Messages about an assignment's list of criteria sometimes contain the words "contact the system administrator" («зверніться до адміністратора системи») — it is the same platform administrator. There is no button next to these messages, so write to the address yourself.

### What to add to the email {#contact-letter}

To get help faster, add to the email:

- the name of the course and the section;
- the name of the material or test, if it is about one;
- when it happened — the date and approximate time;
- the exact text of the message you saw;
- a screenshot.

Important: never send your access key — neither in the email nor in a screenshot.

Back to the section — [For authors](index.md).
