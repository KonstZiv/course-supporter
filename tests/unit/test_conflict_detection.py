"""Tests for conflict detection (subtree overlap)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from course_supporter.conflict_detection import (
    ConflictInfo,
    detect_conflict,
)


def _mock_job(
    *,
    job_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock Job with id and node_id."""
    job = MagicMock()
    job.id = job_id or uuid.uuid4()
    job.node_id = node_id
    return job


def _mock_node(
    *,
    node_id: uuid.UUID,
    parent_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock MaterialNode for parent chain walking."""
    node = MagicMock()
    node.id = node_id
    node.parent_id = parent_id
    return node


def _make_session(nodes: dict[uuid.UUID, MagicMock] | None = None) -> AsyncMock:
    """Create an AsyncSession that resolves get() calls from a node dict."""
    session = AsyncMock()
    node_map = nodes or {}

    async def _get(_cls: object, nid: uuid.UUID) -> MagicMock | None:
        return node_map.get(nid)

    session.get.side_effect = _get
    return session


# ── Course ↔ Course ──


class TestCourseCourseConflict:
    """Both scopes are course-level (node_id=None)."""

    async def test_course_vs_course_conflicts(self) -> None:
        """Two course-level generation requests conflict."""
        session = _make_session()
        job = _mock_job(node_id=None)

        result = await detect_conflict(
            session,
            course_id=uuid.uuid4(),
            target_node_id=None,
            active_jobs=[job],
        )

        assert result is not None
        assert result.job_id == job.id
        assert "entire course" in result.reason


# ── Course ↔ Node ──


class TestCourseNodeConflict:
    """Course-level job vs node-level request (and vice-versa)."""

    async def test_active_course_vs_target_node(self) -> None:
        """Active course-level job conflicts with node request."""
        session = _make_session()
        job = _mock_job(node_id=None)

        result = await detect_conflict(
            session,
            course_id=uuid.uuid4(),
            target_node_id=uuid.uuid4(),
            active_jobs=[job],
        )

        assert result is not None
        assert "active job covers entire course" in result.reason

    async def test_active_node_vs_target_course(self) -> None:
        """Active node-level job conflicts with course-level request."""
        session = _make_session()
        node_id = uuid.uuid4()
        job = _mock_job(node_id=node_id)

        result = await detect_conflict(
            session,
            course_id=uuid.uuid4(),
            target_node_id=None,
            active_jobs=[job],
        )

        assert result is not None
        assert "new request covers entire course" in result.reason


# ── Same Node ──


class TestSameNodeConflict:
    """Both scopes target the same node."""

    async def test_same_node_conflicts(self) -> None:
        """Same node_id in both scopes conflicts."""
        session = _make_session()
        node_id = uuid.uuid4()
        job = _mock_job(node_id=node_id)

        result = await detect_conflict(
            session,
            course_id=uuid.uuid4(),
            target_node_id=node_id,
            active_jobs=[job],
        )

        assert result is not None
        assert "same node" in result.reason


# ── Parent ↔ Child (ancestor chain) ──


class TestParentChildConflict:
    """Active job on parent, target on child (or vice-versa)."""

    async def test_active_parent_target_child(self) -> None:
        """Active job on Node A, target is A's child → conflict."""
        parent_id = uuid.uuid4()
        child_id = uuid.uuid4()
        parent_node = _mock_node(node_id=parent_id, parent_id=None)
        child_node = _mock_node(node_id=child_id, parent_id=parent_id)
        session = _make_session({parent_id: parent_node, child_id: child_node})

        job = _mock_job(node_id=parent_id)
        result = await detect_conflict(
            session,
            course_id=uuid.uuid4(),
            target_node_id=child_id,
            active_jobs=[job],
        )

        assert result is not None
        assert "nested inside active job" in result.reason

    async def test_active_child_target_parent(self) -> None:
        """Active job on child, target is parent → conflict."""
        parent_id = uuid.uuid4()
        child_id = uuid.uuid4()
        parent_node = _mock_node(node_id=parent_id, parent_id=None)
        child_node = _mock_node(node_id=child_id, parent_id=parent_id)
        session = _make_session({parent_id: parent_node, child_id: child_node})

        job = _mock_job(node_id=child_id)
        result = await detect_conflict(
            session,
            course_id=uuid.uuid4(),
            target_node_id=parent_id,
            active_jobs=[job],
        )

        assert result is not None
        assert "nested inside target" in result.reason

    async def test_active_grandparent_target_grandchild(self) -> None:
        """Active job on root, target is grandchild → conflict."""
        root_id = uuid.uuid4()
        child_id = uuid.uuid4()
        grandchild_id = uuid.uuid4()

        root_node = _mock_node(node_id=root_id, parent_id=None)
        child_node = _mock_node(node_id=child_id, parent_id=root_id)
        grandchild_node = _mock_node(node_id=grandchild_id, parent_id=child_id)
        session = _make_session(
            {root_id: root_node, child_id: child_node, grandchild_id: grandchild_node}
        )

        job = _mock_job(node_id=root_id)
        result = await detect_conflict(
            session,
            course_id=uuid.uuid4(),
            target_node_id=grandchild_id,
            active_jobs=[job],
        )

        assert result is not None
        assert "nested inside active job" in result.reason


# ── Siblings (no conflict) ──


class TestSiblingsNoConflict:
    """Independent subtrees do not conflict."""

    async def test_siblings_no_conflict(self) -> None:
        """Node A1 and Node A2 are siblings → no conflict."""
        parent_id = uuid.uuid4()
        sibling_a = uuid.uuid4()
        sibling_b = uuid.uuid4()

        parent_node = _mock_node(node_id=parent_id, parent_id=None)
        node_a = _mock_node(node_id=sibling_a, parent_id=parent_id)
        node_b = _mock_node(node_id=sibling_b, parent_id=parent_id)
        session = _make_session(
            {parent_id: parent_node, sibling_a: node_a, sibling_b: node_b}
        )

        job = _mock_job(node_id=sibling_a)
        result = await detect_conflict(
            session,
            course_id=uuid.uuid4(),
            target_node_id=sibling_b,
            active_jobs=[job],
        )

        assert result is None

    async def test_independent_branches_no_conflict(self) -> None:
        """Nodes in separate branches of the tree → no conflict."""
        root_id = uuid.uuid4()
        branch_a_id = uuid.uuid4()
        branch_b_id = uuid.uuid4()
        leaf_a_id = uuid.uuid4()
        leaf_b_id = uuid.uuid4()

        nodes = {
            root_id: _mock_node(node_id=root_id, parent_id=None),
            branch_a_id: _mock_node(node_id=branch_a_id, parent_id=root_id),
            branch_b_id: _mock_node(node_id=branch_b_id, parent_id=root_id),
            leaf_a_id: _mock_node(node_id=leaf_a_id, parent_id=branch_a_id),
            leaf_b_id: _mock_node(node_id=leaf_b_id, parent_id=branch_b_id),
        }
        session = _make_session(nodes)

        job = _mock_job(node_id=leaf_a_id)
        result = await detect_conflict(
            session,
            course_id=uuid.uuid4(),
            target_node_id=leaf_b_id,
            active_jobs=[job],
        )

        assert result is None


# ── No active jobs ──


class TestNoActiveJobs:
    """No conflict when there are no active jobs."""

    async def test_empty_active_jobs(self) -> None:
        """Empty active_jobs list → no conflict."""
        session = _make_session()

        result = await detect_conflict(
            session,
            course_id=uuid.uuid4(),
            target_node_id=uuid.uuid4(),
            active_jobs=[],
        )

        assert result is None

    async def test_course_level_no_active_jobs(self) -> None:
        """Course-level request with no active jobs → no conflict."""
        session = _make_session()

        result = await detect_conflict(
            session,
            course_id=uuid.uuid4(),
            target_node_id=None,
            active_jobs=[],
        )

        assert result is None


# ── Multiple active jobs ──


class TestMultipleActiveJobs:
    """Detect conflict among multiple active jobs."""

    async def test_first_conflicting_job_returned(self) -> None:
        """Returns info about the first conflicting job."""
        session = _make_session()
        node_id = uuid.uuid4()
        job1 = _mock_job(node_id=uuid.uuid4())  # different node
        job2 = _mock_job(node_id=node_id)  # same node — conflict

        # job1 is in different subtree, but both are non-None so need DB walk.
        # For simplicity make job1 unresolvable (node not in DB) — no overlap.
        result = await detect_conflict(
            session,
            course_id=uuid.uuid4(),
            target_node_id=node_id,
            active_jobs=[job1, job2],
        )

        assert result is not None
        assert result.job_id == job2.id

    async def test_no_conflict_among_multiple_independent(self) -> None:
        """Multiple active jobs on independent nodes → no conflict."""
        parent_id = uuid.uuid4()
        sib_a = uuid.uuid4()
        sib_b = uuid.uuid4()
        sib_c = uuid.uuid4()

        nodes = {
            parent_id: _mock_node(node_id=parent_id, parent_id=None),
            sib_a: _mock_node(node_id=sib_a, parent_id=parent_id),
            sib_b: _mock_node(node_id=sib_b, parent_id=parent_id),
            sib_c: _mock_node(node_id=sib_c, parent_id=parent_id),
        }
        session = _make_session(nodes)

        jobs = [_mock_job(node_id=sib_a), _mock_job(node_id=sib_b)]
        result = await detect_conflict(
            session,
            course_id=uuid.uuid4(),
            target_node_id=sib_c,
            active_jobs=jobs,
        )

        assert result is None


# ── ConflictInfo dataclass ──


class TestConflictInfoFields:
    """ConflictInfo captures correct fields."""

    async def test_conflict_info_fields(self) -> None:
        """ConflictInfo has job_id, job_node_id, reason."""
        session = _make_session()
        node_id = uuid.uuid4()
        job = _mock_job(node_id=node_id)

        result = await detect_conflict(
            session,
            course_id=uuid.uuid4(),
            target_node_id=node_id,
            active_jobs=[job],
        )

        assert result is not None
        assert isinstance(result, ConflictInfo)
        assert result.job_id == job.id
        assert result.job_node_id == node_id
        assert isinstance(result.reason, str)
