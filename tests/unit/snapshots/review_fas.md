# بازبینی

## پذیرفته نشد

Two of the four requirements are met.

## اصلاح شد

*فایل main.py*

- **مشکل چیست:** The loop read past the end of the list.
- **چرا این موضوع مهم است:** It crashed on an empty input.
- **چه کاری باید انجام داد:** It is fixed — the bound is now len(items).

## ملاحظات جدید

*ویدیو، 03:41*

- **مشکل چیست:** The password is written in the source.
- **چرا این موضوع مهم است:** Everyone who opens the file can see it.
- **چه کاری باید انجام داد:** Move it into an environment variable.
- **منابع برای مطالعه:** [Keeping secrets](https://example.test/s), [Environment](https://example.test/e)

*پاراگراف 7*

- **مشکل چیست:** The function does three things.
- **چرا این موضوع مهم است:** A reader has to hold all three at once.
- **چه کاری باید انجام داد:** Split it where the comments already divide it.

## باز مانده

*اسلاید 12*

- **مشکل چیست:** There are still no tests for the error path.
- **چرا این موضوع مهم است:** A change there would break silently.
- **چه کاری باید انجام داد:** Add one test for the empty input.

## خراب شده

- **مشکل چیست:** Sorting no longer keeps equal items in order.
- **چرا این موضوع مهم است:** It worked in the previous submission.
- **چه کاری باید انجام داد:** Use a stable sort.

## از طرف مربی

You are close. The structure is right; the details are not yet.

## پاسخ به نظرات شما

- **سوال شما:** Why is a global variable bad here?
- **پاسخ:** Because two parts of the program can change it at once.

- **اعتراض شما:** The tutorial does it this way.
- **پاسخ:** It does, and it says that is for brevity, not for production.

- **نظر شما:** This took me longer than I expected.
- **پاسخ:** It takes everyone longer. The second time it will not.

## نحوه بررسی

**با اجرا بررسی شد:**

- The program starts and answers on the given input.
- The test for the empty input fails.

**با مطالعه بررسی شد:**

- The names follow the convention of the course.
- The error path is not covered.

## پیشرفت شما

Third submission in a row with no remarks about style.
