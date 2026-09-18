# Рецензія

## Не зараховано

Two of the four requirements are met.

## Виправлено

*файл main.py*

- **Що не так:** The loop read past the end of the list.
- **Чому це важливо:** It crashed on an empty input.
- **Що зробити:** It is fixed — the bound is now len(items).

## Нові зауваження

*відео, 03:41*

- **Що не так:** The password is written in the source.
- **Чому це важливо:** Everyone who opens the file can see it.
- **Що зробити:** Move it into an environment variable.
- **Де почитати:** [Keeping secrets](https://example.test/s), [Environment](https://example.test/e)

*абзац 7*

- **Що не так:** The function does three things.
- **Чому це важливо:** A reader has to hold all three at once.
- **Що зробити:** Split it where the comments already divide it.

## Лишилося відкритим

*слайд 12*

- **Що не так:** There are still no tests for the error path.
- **Чому це важливо:** A change there would break silently.
- **Що зробити:** Add one test for the empty input.

## Зламалося

- **Що не так:** Sorting no longer keeps equal items in order.
- **Чому це важливо:** It worked in the previous submission.
- **Що зробити:** Use a stable sort.

## Від Ментора

You are close. The structure is right; the details are not yet.

## Відповіді на ваші репліки

- **Ваше питання:** Why is a global variable bad here?
- **Відповідь:** Because two parts of the program can change it at once.

- **Ваше заперечення:** The tutorial does it this way.
- **Відповідь:** It does, and it says that is for brevity, not for production.

- **Ваш коментар:** This took me longer than I expected.
- **Відповідь:** It takes everyone longer. The second time it will not.

## Як перевірено

**Перевірено запуском:**

- The program starts and answers on the given input.
- The test for the empty input fails.

**Перевірено читанням:**

- The names follow the convention of the course.
- The error path is not covered.

## Ваш прогрес

Third submission in a row with no remarks about style.
