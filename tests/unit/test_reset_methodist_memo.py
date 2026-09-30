"""CLI of ``scripts.reset_methodist_memo``: preview by default, reset on --apply.

The queries themselves run against a real database in
``tests/integration/test_methodist_full_methodology_db.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from scripts import reset_methodist_memo as script


@pytest.fixture()
def session() -> MagicMock:
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    return session


def _plan() -> script.ResetPlan:
    root = SimpleNamespace(id=uuid.uuid4(), title="Курс")
    node = SimpleNamespace(id=uuid.uuid4(), title="Заняття 1")
    raw = SimpleNamespace(
        id=uuid.uuid4(), source_content_hash="abc", enclosing_context_source_hash=None
    )
    return script.ResetPlan(
        course_root=root,  # type: ignore[arg-type]
        nodes=[node],  # type: ignore[list-item]
        raws={node.id: raw},  # type: ignore[dict-item]
    )


def test_preview_is_the_default(
    session: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch.object(script, "get_sync_session", return_value=session),
        patch.object(script, "plan_reset", return_value=_plan()),
        patch.object(script, "apply_reset") as apply_reset,
    ):
        assert script.main([str(uuid.uuid4())]) == 0

    apply_reset.assert_not_called()
    session.commit.assert_not_called()
    out = capsys.readouterr().out
    assert "Заняття 1: reset" in out
    assert "Preview only: nothing changed" in out


def test_apply_resets(session: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    plan = _plan()
    with (
        patch.object(script, "get_sync_session", return_value=session),
        patch.object(script, "plan_reset", return_value=plan),
        patch.object(script, "apply_reset", return_value=1) as apply_reset,
    ):
        assert script.main([str(uuid.uuid4()), "--apply"]) == 0

    apply_reset.assert_called_once_with(session, plan)
    assert "need approval again" in capsys.readouterr().out


def test_node_id_is_required() -> None:
    with pytest.raises(SystemExit):
        script.main([])


def test_plan_error_exits_non_zero(
    session: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch.object(script, "get_sync_session", return_value=session),
        patch.object(script, "plan_reset", side_effect=script.ResetError("nope")),
    ):
        assert script.main([str(uuid.uuid4()), "--apply"]) == 1

    assert "Error: nope" in capsys.readouterr().err
