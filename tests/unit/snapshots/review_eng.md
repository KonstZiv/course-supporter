# Review

## Not passed

Score: 50%

Two of the four requirements are met.

## Test questions

Explanations are given in the language of the course.

**Question 1:** Correct

**Question 2:** Incorrect

- **Correct answer:** б) The text the model sees in one call
- The window is what the model reads at once.

**Question 3:** Incorrect

- **Correct answer:** а) Yes; в) Only with a key
- No explanation for this question.

**Question 4:** Correct

Try the test again.

## Fixed

*file main.py*

- **What's wrong:** The loop read past the end of the list.
- **Why it matters:** It crashed on an empty input.
- **What to do:** It is fixed — the bound is now len(items).

## New remarks

*video, 03:41*

- **What's wrong:** The password is written in the source.
- **Why it matters:** Everyone who opens the file can see it.
- **What to do:** Move it into an environment variable.
- **Read more:** [Keeping secrets](https://example.test/s), [Environment](https://example.test/e)

*paragraph 7*

- **What's wrong:** The function does three things.
- **Why it matters:** A reader has to hold all three at once.
- **What to do:** Split it where the comments already divide it.

## Still open

*slide 12*

- **What's wrong:** There are still no tests for the error path.
- **Why it matters:** A change there would break silently.
- **What to do:** Add one test for the empty input.

## No longer works

- **What's wrong:** Sorting no longer keeps equal items in order.
- **Why it matters:** It worked in the previous submission.
- **What to do:** Use a stable sort.

## From the Mentor

You are close. The structure is right; the details are not yet.

## Answers to your replies

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
