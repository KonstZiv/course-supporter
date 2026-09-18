# Review

## Failed

Two of the four requirements are met.

## Fixed

*file main.py*

- **What's wrong:** The loop read past the end of the list.
- **Why it's important:** It crashed on an empty input.
- **What to do:** It is fixed — the bound is now len(items).

## New remarks

*video, 03:41*

- **What's wrong:** The password is written in the source.
- **Why it's important:** Everyone who opens the file can see it.
- **What to do:** Move it into an environment variable.
- **Where to read:** [Keeping secrets](https://example.test/s), [Environment](https://example.test/e)

*paragraph 7*

- **What's wrong:** The function does three things.
- **Why it's important:** A reader has to hold all three at once.
- **What to do:** Split it where the comments already divide it.

## Remains open

*slide 12*

- **What's wrong:** There are still no tests for the error path.
- **Why it's important:** A change there would break silently.
- **What to do:** Add one test for the empty input.

## Broken

- **What's wrong:** Sorting no longer keeps equal items in order.
- **Why it's important:** It worked in the previous submission.
- **What to do:** Use a stable sort.

## From the Mentor

You are close. The structure is right; the details are not yet.

## Replies to your comments

- **Your question:** Why is a global variable bad here?
- **Answer:** Because two parts of the program can change it at once.

- **Your objection:** The tutorial does it this way.
- **Answer:** It does, and it says that is for brevity, not for production.

- **Your comment:** This took me longer than I expected.
- **Answer:** It takes everyone longer. The second time it will not.

## How it was checked

**Checked by running:**

- The program starts and answers on the given input.
- The test for the empty input fails.

**Checked by reading:**

- The names follow the convention of the course.
- The error path is not covered.

## Your progress

Third submission in a row with no remarks about style.
