"""Open courses (O-1) through the API on a live database.

Locks of the task:

* (б) the author's link: open / rotate / close; the old key after rotate is a
  404 on the card and on signup; another author's course and a child node are
  a 404.
* (в) signup refusals: no adult mark, another terms revision, a taken login;
  the answer has the same form for a new email and one already in the tenant.
* (г) nothing before confirm — no student, login, enrollment, billing
  account; confirm creates all of it at once, with the consent and the adult
  mark; confirm again — 404; expired — 404.
* (ґ) the login taken between signup and confirm — 409, the signup's login is
  changed, confirm goes through.
* (д) a student with a login: no confirmed email — 409; joined — 201 'self';
  again — already_enrolled.
* (и) the language: a signup in ``en`` → the email in English, the student's
  ``interface_language`` ``eng``; an unknown language — 422.
* (з) the author's routes keep their answers (plus ``source``).

Requires ``docker compose up -d`` (PostgreSQL), migrated to head; ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute
from structlog.testing import capture_logs

from course_supporter.config import get_settings
from course_supporter.services.email_text import render_email
from course_supporter.storage.orm import (
    BillingAccount,
    BillingConsent,
    CourseNode,
    PortalSignup,
    Student,
    StudentCredential,
    StudentEnrollment,
)
from tests._helpers.open_courses import (
    TEXT_SHA,
    FakeArq,
    World,
    app_client,
    auth,
    confirm,
    drop_world,
    enroll_by_link,
    make_world,
    open_link,
    portal_token,
    provision,
    signup,
    signup_body,
    terms,
)

pytestmark = pytest.mark.requires_db


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[World]:
    made = await make_world(session_factory)
    yield made
    await drop_world(session_factory, made)


@pytest.fixture()
def arq() -> FakeArq:
    return FakeArq()


@pytest.fixture()
async def client(
    world: World,
    session_factory: async_sessionmaker[AsyncSession],
    arq: FakeArq,
) -> AsyncIterator[AsyncClient]:
    async with app_client(world, session_factory, arq) as http:
        yield http


async def _count(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_column: InstrumentedAttribute[Any],
    tenant_id: uuid.UUID,
) -> int:
    """Rows of the column's table in the tenant."""
    async with session_factory() as session:
        found = await session.scalar(
            select(func.count()).where(tenant_column == tenant_id)
        )
        return int(found or 0)


def _code(resp: Response) -> str:
    return str(resp.json()["detail"]["code"])


# ── (б) The author's link ───────────────────────────────────────────────────


class TestAuthorLink:
    async def test_open_rotate_close(self, client: AsyncClient, world: World) -> None:
        base = f"/api/v1/nodes/{world.course}/enrollment-link"
        closed = await client.get(base)
        assert closed.json() == {"open": False, "key": None, "url": None}

        with capture_logs() as logs:
            opened = (await client.post(f"{base}/open")).json()
            again = (await client.post(f"{base}/open")).json()
            rotated = (await client.post(f"{base}/rotate")).json()
            shut = (await client.post(f"{base}/close")).json()
        assert opened["open"] is True and len(opened["key"]) >= 22
        assert opened["url"].endswith(f"/{world.tenant_a}/join/{opened['key']}")
        assert again == opened  # opening an open link changes nothing
        assert rotated["key"] != opened["key"]
        assert shut == {"open": False, "key": None, "url": None}

        changes = [e for e in logs if e["event"].startswith("course_enrollment_link")]
        assert [e["event"] for e in changes] == [
            "course_enrollment_link_opened",
            "course_enrollment_link_rotated",
            "course_enrollment_link_closed",
        ]
        for line in changes:
            assert line["tenant_id"] == str(world.tenant_a)
            assert line["course_id"] == str(world.course)
            assert opened["key"] not in str(line) and rotated["key"] not in str(line)

    async def test_the_old_key_after_rotate_is_404(
        self, client: AsyncClient, world: World
    ) -> None:
        old = await open_link(client, world.course)
        new = (
            await client.post(f"/api/v1/nodes/{world.course}/enrollment-link/rotate")
        ).json()["key"]

        card_old = await client.get(f"/api/v1/portal/join/{world.tenant_a}/{old}")
        assert card_old.status_code == 404
        assert _code(card_old) == "course_not_found"
        refused = await signup(client, world.tenant_a, old, "late", "late@example.com")
        assert refused.status_code == 404
        assert _code(refused) == "course_not_found"

        card = await client.get(f"/api/v1/portal/join/{world.tenant_a}/{new}")
        assert card.status_code == 200, card.text
        assert card.json() == {
            "course_id": str(world.course),
            "course_title": "Python basics",
            "author_name": world.tenant_a_name,
            "languages": ["ukr"],
            "terms_version": terms(),
        }

    async def test_the_card_gives_the_terms_in_force_and_signup_takes_them(
        self, client: AsyncClient, world: World
    ) -> None:
        key = await open_link(client, world.course)
        card = await client.get(f"/api/v1/portal/join/{world.tenant_a}/{key}")
        assert card.status_code == 200, card.text
        version = card.json()["terms_version"]
        assert version == get_settings().billing_terms_version
        resp = await signup(
            client,
            world.tenant_a,
            key,
            "reader",
            "r@example.com",
            terms_version=version,
        )
        assert resp.status_code == 202, resp.text

    async def test_closed_unknown_and_other_tenant_are_one_404(
        self, client: AsyncClient, world: World
    ) -> None:
        key = await open_link(client, world.course)
        answers = [
            await client.get(f"/api/v1/portal/join/{world.tenant_b}/{key}"),
            await client.get(f"/api/v1/portal/join/{world.tenant_a}/nope"),
        ]
        await client.post(f"/api/v1/nodes/{world.course}/enrollment-link/close")
        answers.append(await client.get(f"/api/v1/portal/join/{world.tenant_a}/{key}"))
        assert {a.status_code for a in answers} == {404}
        assert len({a.text for a in answers}) == 1

    @pytest.mark.parametrize("which", ["course_b", "child"])
    @pytest.mark.parametrize("action", ["", "/open", "/rotate", "/close"])
    async def test_another_authors_course_or_a_child_is_404(
        self, client: AsyncClient, world: World, which: str, action: str
    ) -> None:
        node = getattr(world, which)
        url = f"/api/v1/nodes/{node}/enrollment-link{action}"
        resp = await (client.get(url) if not action else client.post(url))
        assert resp.status_code == 404

    async def test_closing_keeps_those_enrolled(
        self, client: AsyncClient, world: World, arq: FakeArq
    ) -> None:
        key = await open_link(client, world.course)
        done = await enroll_by_link(
            client, arq, world.tenant_a, key, "kept", "kept@example.com"
        )
        await client.post(f"/api/v1/nodes/{world.course}/enrollment-link/close")
        courses = await client.get(
            "/api/v1/portal/courses", headers=auth(done["access_token"])
        )
        assert [c["id"] for c in courses.json()] == [str(world.course)]


# ── (в) Signup refusals ─────────────────────────────────────────────────────


class TestSignupRefusals:
    async def test_without_the_adult_mark_422(
        self, client: AsyncClient, world: World
    ) -> None:
        key = await open_link(client, world.course)
        url = f"/api/v1/portal/join/{world.tenant_a}/{key}/signup"
        false = await client.post(
            url, json=signup_body("kid", "k@example.com", adult=False)
        )
        body = signup_body("kid", "k@example.com")
        del body["adult"]
        missing = await client.post(url, json=body)
        assert (false.status_code, missing.status_code) == (422, 422)

    async def test_another_terms_revision_409(
        self, client: AsyncClient, world: World
    ) -> None:
        key = await open_link(client, world.course)
        resp = await signup(
            client,
            world.tenant_a,
            key,
            "old",
            "o@example.com",
            terms_version="2000-01-01",
        )
        assert resp.status_code == 409
        assert _code(resp) == "terms_changed"

    async def test_a_weak_password_422(self, client: AsyncClient, world: World) -> None:
        key = await open_link(client, world.course)
        resp = await signup(
            client, world.tenant_a, key, "weak", "w@example.com", password="short"
        )
        assert resp.status_code == 422

    async def test_a_login_taken_by_a_login_or_a_pending_signup_409(
        self, client: AsyncClient, world: World
    ) -> None:
        key = await open_link(client, world.course)
        await provision(client, "taken")
        by_login = await signup(client, world.tenant_a, key, "taken", "t@example.com")
        assert by_login.status_code == 409
        assert _code(by_login) == "login_taken"

        first = await signup(client, world.tenant_a, key, "pending", "p1@example.com")
        assert first.status_code == 202
        by_signup = await signup(
            client, world.tenant_a, key, "pending", "p2@example.com"
        )
        assert by_signup.status_code == 409
        assert _code(by_signup) == "login_taken"

    async def test_an_expired_signup_does_not_hold_its_login(
        self,
        client: AsyncClient,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        key = await open_link(client, world.course)
        first = await signup(client, world.tenant_a, key, "again", "a1@example.com")
        async with session_factory() as session:
            await session.execute(
                update(PortalSignup)
                .where(PortalSignup.id == uuid.UUID(first.json()["signup_id"]))
                .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
            )
            await session.commit()
        second = await signup(client, world.tenant_a, key, "again", "a2@example.com")
        assert second.status_code == 202, second.text

    async def test_the_answer_is_the_same_for_a_known_and_a_new_email(
        self,
        client: AsyncClient,
        world: World,
        arq: FakeArq,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        key = await open_link(client, world.course)
        await enroll_by_link(
            client, arq, world.tenant_a, key, "sib1", "fam@example.com"
        )
        known = await signup(client, world.tenant_a, key, "sib2", "fam@example.com")
        new = await signup(client, world.tenant_a, key, "solo", "solo@example.com")
        assert (known.status_code, new.status_code) == (202, 202)
        assert known.json().keys() == new.json().keys()
        assert known.json()["email_masked"] == "f***@e***.com"
        assert len(arq.emails()) == 3  # each signup mails its link
        async with session_factory() as session:
            pending = await session.scalar(
                select(func.count())
                .select_from(PortalSignup)
                .where(
                    PortalSignup.tenant_id == world.tenant_a,
                    PortalSignup.confirmed_at.is_(None),
                )
            )
        assert pending == 2


# ── (г) Confirm ─────────────────────────────────────────────────────────────


class TestConfirm:
    async def test_nothing_before_confirm_everything_at_confirm(
        self,
        client: AsyncClient,
        world: World,
        arq: FakeArq,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        key = await open_link(client, world.course)
        resp = await signup(
            client, world.tenant_a, key, "newbie", "New@Example.com", display_name="N"
        )
        assert resp.status_code == 202, resp.text
        assert resp.json()["email_masked"] == "n***@e***.com"
        for column in (
            Student.tenant_id,
            StudentCredential.tenant_id,
            BillingAccount.tenant_id,
        ):
            assert await _count(session_factory, column, world.tenant_a) == 0
        async with session_factory() as session:
            enrolled = await session.scalar(
                select(func.count())
                .select_from(StudentEnrollment)
                .where(StudentEnrollment.course_node_id == world.course)
            )
        assert enrolled == 0
        roster = await client.get("/api/v1/students")
        assert roster.json()["total"] == 0

        done = await confirm(client, arq.last_token())
        assert done.status_code == 200, done.text
        body = done.json()
        assert body["course_id"] == str(world.course)
        assert body["tenant_id"] == str(world.tenant_a)
        assert body["display_name"] == "N"
        student_id = uuid.UUID(body["student_id"])

        async with session_factory() as session:
            student = await session.get(Student, student_id)
            credential = await session.scalar(
                select(StudentCredential).where(
                    StudentCredential.student_id == student_id
                )
            )
            enrollment = await session.scalar(
                select(StudentEnrollment).where(
                    StudentEnrollment.student_id == student_id
                )
            )
            consent = await session.scalar(
                select(BillingConsent)
                .join(BillingAccount, BillingConsent.account_id == BillingAccount.id)
                .where(BillingAccount.student_id == student_id)
            )
            signup_row = await session.scalar(
                select(PortalSignup).where(PortalSignup.student_id == student_id)
            )
        assert student is not None and student.adult_confirmed_at is not None
        assert credential is not None
        assert credential.login == "newbie"
        assert credential.recovery_email == "new@example.com"
        assert credential.recovery_email_confirmed_at is not None
        assert enrollment is not None and enrollment.source == "self"
        assert enrollment.course_node_id == world.course
        assert consent is not None
        assert (consent.terms_version, consent.text_sha256, consent.channel) == (
            terms(),
            TEXT_SHA,
            "portal",
        )
        assert signup_row is not None and signup_row.confirmed_at is not None

        me = await client.get("/api/v1/portal/me", headers=auth(body["access_token"]))
        assert me.json()["recovery_email_confirmed"] is True
        assert await portal_token(client, world.tenant_a, "newbie")  # the password

    async def test_confirm_twice_or_expired_is_404(
        self,
        client: AsyncClient,
        world: World,
        arq: FakeArq,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        key = await open_link(client, world.course)
        await signup(client, world.tenant_a, key, "twice", "twice@example.com")
        token = arq.last_token()
        assert (await confirm(client, token)).status_code == 200
        again = await confirm(client, token)
        assert again.status_code == 404
        assert _code(again) == "signup_not_found"

        expired = await signup(client, world.tenant_a, key, "late", "late@example.com")
        async with session_factory() as session:
            await session.execute(
                update(PortalSignup)
                .where(PortalSignup.id == uuid.UUID(expired.json()["signup_id"]))
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        late = await confirm(client, arq.last_token())
        assert late.status_code == 404
        assert _code(late) == "signup_not_found"

    async def test_resend_replaces_the_link(
        self, client: AsyncClient, world: World, arq: FakeArq
    ) -> None:
        key = await open_link(client, world.course)
        made = await signup(client, world.tenant_a, key, "resent", "r@example.com")
        first = arq.last_token()
        again = await client.post(
            "/api/v1/portal/join/resend", json={"signup_id": made.json()["signup_id"]}
        )
        assert again.status_code == 202, again.text
        assert again.json()["expires_at"] == made.json()["expires_at"]
        second = arq.last_token()
        assert second != first
        assert (await confirm(client, first)).status_code == 404
        assert (await confirm(client, second)).status_code == 200
        gone = await client.post(
            "/api/v1/portal/join/resend", json={"signup_id": made.json()["signup_id"]}
        )
        assert gone.status_code == 404

    async def test_a_course_closed_after_signup_is_still_enrolled(
        self, client: AsyncClient, world: World, arq: FakeArq
    ) -> None:
        key = await open_link(client, world.course)
        await signup(client, world.tenant_a, key, "closed", "c@example.com")
        await client.post(f"/api/v1/nodes/{world.course}/enrollment-link/close")
        done = await confirm(client, arq.last_token())
        assert done.status_code == 200
        assert done.json()["course_id"] == str(world.course)


# ── (ґ) The login taken between signup and confirm ──────────────────────────


class TestLoginTakenAtConfirm:
    async def test_409_then_another_login_then_confirm(
        self,
        client: AsyncClient,
        world: World,
        arq: FakeArq,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        key = await open_link(client, world.course)
        made = await signup(client, world.tenant_a, key, "wanted", "w@example.com")
        signup_id = made.json()["signup_id"]
        token = arq.last_token()
        await provision(client, "wanted")  # the author gives the login away
        await provision(client, "also-taken")

        taken = await confirm(client, token)
        assert taken.status_code == 409
        assert taken.json()["detail"]["code"] == "login_taken"
        assert taken.json()["detail"]["signup_id"] == signup_id

        url = f"/api/v1/portal/join/{signup_id}/login"
        busy = await client.post(url, json={"login": "also-taken", "token": token})
        assert busy.status_code == 409
        wrong = await client.post(url, json={"login": "free", "token": "nope"})
        assert wrong.status_code == 404
        changed = await client.post(url, json={"login": "free", "token": token})
        assert changed.status_code == 204, changed.text

        done = await confirm(client, token)
        assert done.status_code == 200, done.text
        assert await portal_token(client, world.tenant_a, "free")
        async with session_factory() as session:
            row = await session.get(PortalSignup, uuid.UUID(signup_id))
        assert row is not None and row.login == "free"


# ── (д) A student with a login joins by the link ────────────────────────────


class TestExistingStudentJoins:
    async def _join(
        self, client: AsyncClient, token: str, tenant_id: uuid.UUID, key: str
    ) -> Response:
        return await client.post(
            "/api/v1/portal/courses/join",
            json={
                "tenant_id": str(tenant_id),
                "key": key,
                "adult": True,
                "terms_version": terms(),
                "text_sha256": TEXT_SHA,
            },
            headers=auth(token),
        )

    async def test_email_first_then_join_then_already(
        self,
        client: AsyncClient,
        world: World,
        arq: FakeArq,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        key = await open_link(client, world.course)
        student_id = uuid.UUID(await provision(client, "veteran"))
        token = await portal_token(client, world.tenant_a, "veteran")

        need = await self._join(client, token, world.tenant_a, key)
        assert need.status_code == 409
        assert _code(need) == "email_confirmation_required"

        await client.post(
            "/api/v1/portal/recovery-email",
            json={"email": "vet@example.com"},
            headers=auth(token),
        )
        await client.post(
            "/api/v1/portal/recovery-email/confirm",
            json={"token": arq.last_token("email_confirm")},
        )

        joined = await self._join(client, token, world.tenant_a, key)
        assert joined.status_code == 201, joined.text
        assert joined.json() == {"status": "enrolled", "course_id": str(world.course)}
        again = await self._join(client, token, world.tenant_a, key)
        assert again.status_code == 200
        assert again.json()["status"] == "already_enrolled"

        async with session_factory() as session:
            enrollment = await session.scalar(
                select(StudentEnrollment).where(
                    StudentEnrollment.student_id == student_id
                )
            )
            student = await session.get(Student, student_id)
            consents = await session.scalar(
                select(func.count())
                .select_from(BillingConsent)
                .join(BillingAccount, BillingConsent.account_id == BillingAccount.id)
                .where(BillingAccount.student_id == student_id)
            )
        assert enrollment is not None and enrollment.source == "self"
        assert student is not None and student.adult_confirmed_at is not None
        assert consents == 1  # one per account and revision

        listed = await client.get(f"/api/v1/students/{student_id}/enrollments")
        assert [i["source"] for i in listed.json()["items"]] == ["self"]

    async def test_another_authors_link_is_404(
        self,
        client: AsyncClient,
        world: World,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            course_b = await session.get(CourseNode, world.course_b)
            assert course_b is not None
            course_b.enroll_key = f"b-{uuid.uuid4().hex}"
            key_b = course_b.enroll_key
            await session.commit()
        await provision(client, "stranger")
        token = await portal_token(client, world.tenant_a, "stranger")
        resp = await self._join(client, token, world.tenant_b, key_b)
        assert resp.status_code == 404
        assert _code(resp) == "course_not_found"


# ── (и) The language ────────────────────────────────────────────────────────


class TestLanguage:
    async def test_english_signup_english_email_and_eng_student(
        self, client: AsyncClient, world: World, arq: FakeArq
    ) -> None:
        key = await open_link(client, world.course)
        resp = await signup(
            client,
            world.tenant_a,
            key,
            "anglo",
            "en@example.com",
            interface_language="en",
        )
        assert resp.status_code == 202, resp.text
        [mail] = arq.emails()
        assert mail["language"] == "eng"
        assert mail["to"] == "en@example.com"
        rendered = render_email(
            mail["message_id"], language=mail["language"], **mail["context"]
        )
        assert rendered.subject == "Confirm your enrollment in “Python basics”"
        assert "within 7 days" in rendered.body
        assert f"/{world.tenant_a}/login" in rendered.body

        done = await confirm(client, arq.last_token())
        assert done.json()["interface_language"] == "eng"
        me = await client.get(
            "/api/v1/portal/me", headers=auth(done.json()["access_token"])
        )
        assert me.json()["interface_language"] == "eng"

    async def test_no_language_ukrainian_email(
        self, client: AsyncClient, world: World, arq: FakeArq
    ) -> None:
        key = await open_link(client, world.course)
        await signup(client, world.tenant_a, key, "ukr", "uk@example.com")
        [mail] = arq.emails()
        assert mail["language"] is None
        rendered = render_email(mail["message_id"], language=None, **mail["context"])
        assert "протягом 7 днів" in rendered.body

    async def test_an_unknown_language_422(
        self, client: AsyncClient, world: World
    ) -> None:
        key = await open_link(client, world.course)
        resp = await signup(
            client, world.tenant_a, key, "xx", "xx@example.com", interface_language="xx"
        )
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["loc"][-1] == "interface_language"


# ── (з) The author's routes keep their answers ──────────────────────────────


class TestAuthorRoutesUnchanged:
    async def test_bind_list_and_roster_carry_source(
        self, client: AsyncClient, world: World, arq: FakeArq
    ) -> None:
        student_id = await provision(client, "classic")
        bound = await client.post(
            "/api/v1/students/enrollments",
            json={"student_id": student_id, "course_node_id": str(world.course)},
        )
        assert bound.status_code == 201
        assert bound.json() == {
            "student_id": student_id,
            "course_node_id": str(world.course),
            "source": "author",
        }
        listed = await client.get(f"/api/v1/students/{student_id}/enrollments")
        assert [i["source"] for i in listed.json()["items"]] == ["author"]

        key = await open_link(client, world.other_course)
        done = await enroll_by_link(
            client, arq, world.tenant_a, key, "selfie", "s@example.com"
        )
        roster = {
            i["login"]: i
            for i in (await client.get("/api/v1/students")).json()["items"]
        }
        assert roster["classic"]["source"] == "author"
        assert roster["selfie"]["source"] == "self"
        assert roster["selfie"]["student_id"] == done["student_id"]
        assert roster["selfie"]["enrollment_count"] == 1

        card = await client.get(f"/api/v1/portal/join/{world.tenant_a}/{key}")
        assert card.json()["languages"] == ["eng"]
