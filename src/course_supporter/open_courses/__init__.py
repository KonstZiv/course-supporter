"""Open courses (O-1): a student enrolls by the course link the author gives out.

Operator decisions 23.11 (R1-R9): the author opens a course's link (R1, R4),
anyone with it enrolls at once without the author's approval (R2), but only
after confirming their email (R3); the student pays for reviews as before
(R5); a student who already has a login joins with one click (R7); two marks
are required — 18 or older and consent to the terms (R8); the public routes
are rate limited on our server, no captcha, and unconfirmed requests live
7 days (R9). A student with a confirmed email signs in on the shared page by
email and password, into one author's account (variant A).

Modules:
    :mod:`.links`   — the course key, the portal links, the email mask.
    :mod:`.service` — the enrollment link, signup, confirm, joining a course,
                      the login by email.
    :mod:`.cleanup` — the periodic deletion of old requests.
"""
