"""Live acceptance harness for the 6.HC homework cost-attribution surface.

Seeds two tenants' worth of REAL homework-cost rows on the dev DB
(``ExternalServiceCall`` rows attached to real homework ``Job`` +
``HomeworkSubmission``), drives the three ``/cost/homework`` endpoints through
the real ASGI app + real DB, asserts the five acceptance points, then tears the
seed down and verifies zero residue.

Tenant context is overridden per tenant (``get_current_tenant``): the
leak-prevention mechanism under test is the repository's
``HomeworkSubmission.tenant_id`` filter, not the auth layer — so proving that
tenant A's requests never surface tenant B's rows is exactly the meaningful
test. The session and endpoints run against the real dev DB unchanged.

Run (dev DB up)::

    uv run python tools/homework_cost_live_acceptance.py

Teardown runs in a ``finally`` block and re-verifies zero residue — it is part
of acceptance, not an optional afterthought.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import async_session
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Student,
    StudentCredential,
    Tenant,
)

PERIOD_FROM = "2026-01-01"
PERIOD_TO = "2026-12-31"
ESC_TS = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
TENANT_CREATED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
API = "/api/v1"

EPS = 1e-6


def _approx(a: float, b: float) -> bool:
    return abs(a - b) < EPS


class Results:
    """Ordered pass/fail tally with operator-facing printout."""

    def __init__(self) -> None:
        self._rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self._rows.append((name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {name}"
        if detail:
            line += f" — {detail}"
        print(line)

    @property
    def all_passed(self) -> bool:
        return all(ok for _, ok, _ in self._rows)

    def summary(self) -> str:
        passed = sum(1 for _, ok, _ in self._rows if ok)
        return f"{passed}/{len(self._rows)} checks passed"


async def _seed() -> dict[str, Any]:
    """Seed two tenants of homework-cost data. Returns ids + expectations.

    Tenant A (queried) cost layout, all ESC at ``ESC_TS``:

    * Course Alpha (root_A1):
        - task_A1 "hw1.pdf": portal 10+20 (+NULL) = 30, mode-1 5 → 35
        - task_A2 filename=NULL (derived "text #1"): portal 8 → 8
        - task_A3 "deleted-hw.pdf" SOFT-DELETED: portal 12 → 12  (is_deleted)
      → Course Alpha = 55
    * Course Beta (root_A2):
        - task_A4 "beta-hw.pdf": portal 25 → 25
      → Course Beta = 25
    * Excluded from every sum: orphan homework Job ESC 13 (no submission
      references it), ingestion Job ESC 7 (not homework), one NULL-cost ESC,
      one submission with job_id=NULL (no ESC).
    → Tenant A total = 80.0

    Tenant B (isolation control): Course Gamma / task_B1 / student_B → 500.0.
    """
    async with async_session() as s:
        tag = uuid.uuid4().hex[:6]

        tenant_a = Tenant(name=f"6hc-accept-A-{tag}", created_at=TENANT_CREATED_AT)
        tenant_b = Tenant(name=f"6hc-accept-B-{tag}", created_at=TENANT_CREATED_AT)
        s.add_all([tenant_a, tenant_b])
        await s.flush()

        # ── Tenant A course tree ──
        root_a1 = CourseNode(
            tenant_id=tenant_a.id, title="Course Alpha", order=0, default_language="uk"
        )
        root_a2 = CourseNode(
            tenant_id=tenant_a.id, title="Course Beta", order=1, default_language="uk"
        )
        s.add_all([root_a1, root_a2])
        await s.flush()
        node_a1 = CourseNode(
            tenant_id=tenant_a.id, title="Lesson 1", order=0, parent_id=root_a1.id
        )
        s.add(node_a1)
        await s.flush()

        def _task(
            *,
            root: uuid.UUID,
            node: uuid.UUID,
            filename: str | None,
            order: int,
            deleted: bool = False,
        ) -> AuthoredDocument:
            return AuthoredDocument(
                course_node_id=node,
                course_root_id=root,
                source_type="text",
                source_url="s3://seed/task",
                task_type="task",
                filename=filename,
                order=order,
                deleted_at=ESC_TS if deleted else None,
            )

        task_a1 = _task(root=root_a1.id, node=node_a1.id, filename="hw1.pdf", order=0)
        task_a2 = _task(root=root_a1.id, node=node_a1.id, filename=None, order=1)
        task_a3 = _task(
            root=root_a1.id,
            node=node_a1.id,
            filename="deleted-hw.pdf",
            order=2,
            deleted=True,
        )
        task_a4 = _task(
            root=root_a2.id, node=root_a2.id, filename="beta-hw.pdf", order=0
        )
        s.add_all([task_a1, task_a2, task_a3, task_a4])
        await s.flush()

        # ── Tenant A students ──
        student_portal = Student(
            tenant_id=tenant_a.id,
            external_id="ext-portal-1",
            display_name="Іван Порталенко",
        )
        student_mode1 = Student(
            tenant_id=tenant_a.id,
            external_id="ext-mode1-2",
            display_name=None,
        )
        s.add_all([student_portal, student_mode1])
        await s.flush()
        s.add(
            StudentCredential(
                student_id=student_portal.id,
                tenant_id=tenant_a.id,
                login="ivan.login",
                password_hash="seed-placeholder",  # noqa: S106 — seed row, unused by cost
            )
        )

        # ── Tenant B (isolation control) ──
        root_b1 = CourseNode(
            tenant_id=tenant_b.id, title="Course Gamma", order=0, default_language="uk"
        )
        s.add(root_b1)
        await s.flush()
        task_b1 = _task(
            root=root_b1.id, node=root_b1.id, filename="gamma-hw.pdf", order=0
        )
        s.add(task_b1)
        student_b = Student(
            tenant_id=tenant_b.id, external_id="ext-b-1", display_name="Foreign"
        )
        s.add_all([task_b1, student_b])
        await s.flush()

        ad_ids = [task_a1.id, task_a2.id, task_a3.id, task_a4.id, task_b1.id]

        def _job(tenant_id: uuid.UUID, job_type: str = "homework") -> Job:
            # Homework jobs mirror production: course_node_id stays NULL.
            course_node_id = None if job_type == "homework" else node_a1.id
            return Job(
                tenant_id=tenant_id, job_type=job_type, course_node_id=course_node_id
            )

        def _submission(
            *,
            tenant_id: uuid.UUID,
            student_id: uuid.UUID,
            root: uuid.UUID,
            node: uuid.UUID,
            task: uuid.UUID,
            job_id: uuid.UUID | None,
        ) -> HomeworkSubmission:
            return HomeworkSubmission(
                tenant_id=tenant_id,
                student_id=student_id,
                course_node_id=root,
                node_id=node,
                authored_document_id=task,
                file_url="s3://seed/sub",
                file_type="application/pdf",
                job_id=job_id,
                delivery_mode="in_app",
            )

        def _esc(job_id: uuid.UUID, cost: float | None) -> ExternalServiceCall:
            return ExternalServiceCall(
                job_id=job_id,
                provider="deepseek",
                model_id="deepseek-v4",
                cost_usd=cost,
                action="homework_review",
                created_at=ESC_TS,
            )

        # task_A1 / portal — two attempts (30) + a NULL-cost ESC (excluded)
        j_p1 = _job(tenant_a.id)
        j_p2 = _job(tenant_a.id)
        # task_A1 / mode-1 (5) + a second submission with job_id=NULL (no ESC)
        j_m1 = _job(tenant_a.id)
        # task_A2 / portal (8, derived label)
        j_p_a2 = _job(tenant_a.id)
        # task_A3 / portal (12, deleted task)
        j_p_a3 = _job(tenant_a.id)
        # task_A4 / portal (25, second course)
        j_p_a4 = _job(tenant_a.id)
        # orphan homework job (13) referenced by NO submission → excluded
        j_orphan = _job(tenant_a.id)
        # ingestion job (7) — not homework → excluded
        j_ingest = _job(tenant_a.id, job_type="ingest")
        # tenant B homework job (500)
        j_b = _job(tenant_b.id)
        s.add_all([j_p1, j_p2, j_m1, j_p_a2, j_p_a3, j_p_a4, j_orphan, j_ingest, j_b])
        await s.flush()

        s.add_all(
            [
                _submission(
                    tenant_id=tenant_a.id,
                    student_id=student_portal.id,
                    root=root_a1.id,
                    node=node_a1.id,
                    task=task_a1.id,
                    job_id=j_p1.id,
                ),
                _submission(
                    tenant_id=tenant_a.id,
                    student_id=student_portal.id,
                    root=root_a1.id,
                    node=node_a1.id,
                    task=task_a1.id,
                    job_id=j_p2.id,
                ),
                _submission(
                    tenant_id=tenant_a.id,
                    student_id=student_mode1.id,
                    root=root_a1.id,
                    node=node_a1.id,
                    task=task_a1.id,
                    job_id=j_m1.id,
                ),
                # job_id=NULL submission — present but contributes no cost.
                _submission(
                    tenant_id=tenant_a.id,
                    student_id=student_mode1.id,
                    root=root_a1.id,
                    node=node_a1.id,
                    task=task_a1.id,
                    job_id=None,
                ),
                _submission(
                    tenant_id=tenant_a.id,
                    student_id=student_portal.id,
                    root=root_a1.id,
                    node=node_a1.id,
                    task=task_a2.id,
                    job_id=j_p_a2.id,
                ),
                _submission(
                    tenant_id=tenant_a.id,
                    student_id=student_portal.id,
                    root=root_a1.id,
                    node=node_a1.id,
                    task=task_a3.id,
                    job_id=j_p_a3.id,
                ),
                _submission(
                    tenant_id=tenant_a.id,
                    student_id=student_portal.id,
                    root=root_a2.id,
                    node=root_a2.id,
                    task=task_a4.id,
                    job_id=j_p_a4.id,
                ),
                _submission(
                    tenant_id=tenant_b.id,
                    student_id=student_b.id,
                    root=root_b1.id,
                    node=root_b1.id,
                    task=task_b1.id,
                    job_id=j_b.id,
                ),
            ]
        )
        s.add_all(
            [
                _esc(j_p1.id, 10.0),
                _esc(j_p2.id, 20.0),
                _esc(j_p2.id, None),  # NULL cost — excluded from sums
                _esc(j_m1.id, 5.0),
                _esc(j_p_a2.id, 8.0),
                _esc(j_p_a3.id, 12.0),
                _esc(j_p_a4.id, 25.0),
                _esc(j_orphan.id, 13.0),  # no submission → excluded
                _esc(j_ingest.id, 7.0),  # ingestion → excluded
                _esc(j_b.id, 500.0),  # tenant B
            ]
        )
        await s.commit()

        return {
            "tenant_ids": [tenant_a.id, tenant_b.id],
            "ad_ids": ad_ids,
            "tenant_a": tenant_a.id,
            "tenant_b": tenant_b.id,
            "root_a1": root_a1.id,
            "root_a2": root_a2.id,
            "root_b1": root_b1.id,
            "task_a1": task_a1.id,
            "task_a2": task_a2.id,
            "task_a3": task_a3.id,
            "student_portal": student_portal.id,
            "student_mode1": student_mode1.id,
        }


def _ctx(tenant_id: uuid.UUID) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        tenant_name="6hc-accept",
        scopes=["prep", "check"],
        plan_id="basic",
        key_prefix="cs_accept",
    )


async def _assertions(seed: dict[str, Any]) -> Results:
    r = Results()
    params = {"from": PERIOD_FROM, "to": PERIOD_TO}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://accept"
    ) as ac:
        # ── Tenant A perspective ──
        app.dependency_overrides[get_current_tenant] = lambda: _ctx(seed["tenant_a"])

        print("\n(1) Four levels, each a request keyed by the parent:")
        l1 = (await ac.get(f"{API}/cost/homework", params=params)).json()
        r.check(
            "L1 total == 80.0 (excludes orphan 13 + ingestion 7 + NULL)",
            _approx(l1["total_usd"], 80.0),
            f"total_usd={l1['total_usd']}",
        )
        by_course = {c["course_node_id"]: c for c in l1["by_course"]}
        courses_view = [(c["course_title"], c["cost_usd"]) for c in l1["by_course"]]
        r.check(
            "L1 by_course Alpha=55.0 / Beta=25.0 with titles",
            _approx(by_course.get(str(seed["root_a1"]), {}).get("cost_usd", -1), 55.0)
            and _approx(
                by_course.get(str(seed["root_a2"]), {}).get("cost_usd", -1), 25.0
            )
            and by_course[str(seed["root_a1"])]["course_title"] == "Course Alpha",
            f"by_course={courses_view}",
        )

        l2 = (
            await ac.get(f"{API}/cost/homework/course/{seed['root_a1']}", params=params)
        ).json()
        by_task = {t["authored_document_id"]: t for t in l2["by_task"]}
        r.check(
            "L2 task_A1=35.0 label 'hw1.pdf'",
            _approx(by_task.get(str(seed["task_a1"]), {}).get("cost_usd", -1), 35.0)
            and by_task[str(seed["task_a1"])]["task_label"] == "hw1.pdf",
            f"task_A1={by_task.get(str(seed['task_a1']))}",
        )

        l3 = (
            await ac.get(f"{API}/cost/homework/task/{seed['task_a1']}", params=params)
        ).json()
        by_student = {sn["student_id"]: sn for sn in l3["by_student"]}
        portal = by_student.get(str(seed["student_portal"]), {})
        r.check(
            "L3 leaf: portal SUM over 2 attempts == 30.0",
            _approx(portal.get("cost_usd", -1), 30.0),
            f"portal={portal}",
        )

        print("\n(2) Tenant isolation (leak == 0):")
        a_course_ids = {c["course_node_id"] for c in l1["by_course"]}
        r.check(
            "Tenant A L1 has no tenant B course (Gamma / root_b1)",
            str(seed["root_b1"]) not in a_course_ids
            and all(c["course_title"] != "Course Gamma" for c in l1["by_course"])
            and not any(_approx(c["cost_usd"], 500.0) for c in l1["by_course"]),
            f"A course ids={a_course_ids}",
        )
        foreign = (
            await ac.get(f"{API}/cost/homework/course/{seed['root_b1']}", params=params)
        ).json()
        r.check(
            "Tenant A drilling into tenant B course → empty by_task",
            foreign["by_task"] == [],
            f"by_task={foreign['by_task']}",
        )
        app.dependency_overrides[get_current_tenant] = lambda: _ctx(seed["tenant_b"])
        l1_b = (await ac.get(f"{API}/cost/homework", params=params)).json()
        r.check(
            "Tenant B sees only its own 500.0 (no tenant A data)",
            _approx(l1_b["total_usd"], 500.0)
            and all(c["course_title"] == "Course Gamma" for c in l1_b["by_course"]),
            f"B total={l1_b['total_usd']}",
        )

        # Back to tenant A for the remaining checks.
        app.dependency_overrides[get_current_tenant] = lambda: _ctx(seed["tenant_a"])

        print("\n(3) Soft-deleted task shown with is_deleted, cost summed:")
        a3 = by_task.get(str(seed["task_a3"]), {})
        r.check(
            "task_A3 present, is_deleted=true, cost 12.0",
            a3.get("is_deleted") is True and _approx(a3.get("cost_usd", -1), 12.0),
            f"task_A3={a3}",
        )
        a2 = by_task.get(str(seed["task_a2"]), {})
        r.check(
            "task_A2 (filename NULL) derived label 'text #1'",
            a2.get("task_label") == "text #1",
            f"task_A2 label={a2.get('task_label')!r}",
        )

        print("\n(4) mode-1 display fallback:")
        mode1 = by_student.get(str(seed["student_mode1"]), {})
        r.check(
            "portal student display == display_name 'Іван Порталенко'",
            portal.get("student_display") == "Іван Порталенко",
            f"portal display={portal.get('student_display')!r}",
        )
        r.check(
            "mode-1 (no display_name/credential) display == external_id",
            mode1.get("student_display") == "ext-mode1-2",
            f"mode-1 display={mode1.get('student_display')!r}",
        )

        print("\n(5) NULL cost + NULL job_id excluded:")
        # Level totals already exclude the NULL-cost ESC (portal=30 not >30) and
        # the orphan/ingestion ESC (L1 total=80). The job_id=NULL submission is
        # present but adds no cost — mode-1 task_A1 stays 5.0.
        r.check(
            "NULL-cost ESC excluded (portal task_A1 == 30.0, not inflated)",
            _approx(portal.get("cost_usd", -1), 30.0),
        )
        r.check(
            "mode-1 task_A1 == 5.0 (job_id=NULL submission adds nothing)",
            _approx(mode1.get("cost_usd", -1), 5.0),
            f"mode-1 cost={mode1.get('cost_usd')}",
        )

    app.dependency_overrides.clear()
    return r


async def _teardown(seed: dict[str, Any]) -> bool:
    """Delete the whole seed (reverse-FK order) and verify zero residue."""
    tenant_ids = seed["tenant_ids"]
    ad_ids = seed["ad_ids"]
    async with async_session() as s:
        job_ids = select(Job.id).where(Job.tenant_id.in_(tenant_ids))
        await s.execute(
            delete(ExternalServiceCall).where(ExternalServiceCall.job_id.in_(job_ids))
        )
        await s.execute(
            delete(HomeworkSubmission).where(
                HomeworkSubmission.tenant_id.in_(tenant_ids)
            )
        )
        await s.execute(delete(Job).where(Job.tenant_id.in_(tenant_ids)))
        await s.execute(
            delete(StudentCredential).where(StudentCredential.tenant_id.in_(tenant_ids))
        )
        await s.execute(delete(Student).where(Student.tenant_id.in_(tenant_ids)))
        await s.execute(delete(AuthoredDocument).where(AuthoredDocument.id.in_(ad_ids)))
        await s.execute(delete(CourseNode).where(CourseNode.tenant_id.in_(tenant_ids)))
        await s.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
        await s.commit()

        residue = 0
        for cnt_stmt in (
            select(func.count()).select_from(Tenant).where(Tenant.id.in_(tenant_ids)),
            select(func.count())
            .select_from(HomeworkSubmission)
            .where(HomeworkSubmission.tenant_id.in_(tenant_ids)),
            select(func.count()).select_from(Job).where(Job.tenant_id.in_(tenant_ids)),
            select(func.count())
            .select_from(AuthoredDocument)
            .where(AuthoredDocument.id.in_(ad_ids)),
        ):
            residue += (await s.execute(cnt_stmt)).scalar_one()
    ok = residue == 0
    print(f"\nTeardown: residue rows = {residue} ({'clean' if ok else 'LEFTOVER'})")
    return ok


async def main() -> int:
    print("=== 6.HC homework cost-attribution — live acceptance ===")
    seed = await _seed()
    teardown_ok = False
    try:
        results = await _assertions(seed)
    finally:
        app.dependency_overrides.clear()
        teardown_ok = await _teardown(seed)

    print(f"\n{results.summary()}")
    ok = results.all_passed and teardown_ok
    print("RESULT:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
