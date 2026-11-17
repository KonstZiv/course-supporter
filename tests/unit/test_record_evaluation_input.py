"""``scripts/record_evaluation_input.py`` without a network or a database.

The script's sources are swapped for doubles — the submission, the stored
criteria list, the task context, the languages, S3 — and everything after them
is the real code: the doors (``read_text_work``), the prompt variables
(``render_context``), the router that builds the first rung's request, the
registry it reads. The walk through a real database, beside the stage's own
run, is ``tests/integration/test_text_review_e2e_db.py``
(``TestTheRecorderReadsWhatTheStageRead``).
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts import measure_determinism
from scripts import record_evaluation_input as script

from course_supporter.agents.criteria_evaluator import EvaluationInput, render_context
from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.criteria_list_service import CriteriaInForce
from course_supporter.homework.criteria_verdicts import judged_items
from course_supporter.homework.path_config import load_path_config, model_stages
from course_supporter.homework.path_runner import DoorLanguages, read_text_work
from course_supporter.llm.input_hash import hash_attempt_input
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.registry import load_registry
from course_supporter.llm.schemas import LLMRequest, LLMResponse, SchemaMode

_WORK = b"def solve(n):\n    return n * 2\n"
_FILENAME = "answers.py"


def _criterion(cid: str, weight: str, points: int = 0) -> Criterion:
    return Criterion.model_validate(
        {
            "id": cid,
            "text": f"Criterion {cid}",
            "evidence": f"What shows {cid}",
            "weight": weight,
            "check_method": "mandatory_points" if points else "model_verdict",
            "soft_descent": False,
            "concepts": [],
            "mandatory_points": [
                {"id": f"{cid}.p{n}", "text": f"Point {n} of {cid}"}
                for n in range(1, points + 1)
            ],
        }
    )


_CRITERIA = (_criterion("c1", "must"), _criterion("c2", "should", 2))
_IN_FORCE = CriteriaInForce(
    criteria=_CRITERIA, layer=CriteriaLayer.MODEL, source_id=uuid.uuid4()
)
_TASK_CONTEXT = ("Doubling", "Write solve().", "solve(n) returns 2n.")


class _World:
    """The doubles of the script's sources, and what was asked of them."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        task_type: str = "task",
        in_force: CriteriaInForce | None = _IN_FORCE,
        judged: CriteriaInForce | None = None,
        original_filename: str | None = _FILENAME,
    ) -> None:
        self.submission = SimpleNamespace(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            authored_document_id=uuid.uuid4(),
            course_node_id=uuid.uuid4(),
            student_note=None,
            file_url="s3://bucket/homework/answers.py",
            original_filename=original_filename,
        )
        self.task_type = task_type
        self.in_force = in_force
        self.judged = judged
        self.download = tmp_path / "download-1a2b.py"
        self.session = MagicMock()
        self.session.get = AsyncMock(return_value=self.submission)
        self.s3 = MagicMock()
        self.s3.extract_key = MagicMock(return_value="homework/answers.py")
        self.s3.download_file = AsyncMock(side_effect=self._download)

    async def _download(self, _key: str) -> Path:
        self.download.write_bytes(_WORK)
        return self.download

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def door_languages(_session: Any, _submission: Any) -> DoorLanguages:
            return DoorLanguages(
                task=SimpleNamespace(task_type=self.task_type),  # type: ignore[arg-type]
                review="uk",
                verify=["uk"],
            )

        async def load_in_force(_session: Any, _doc: uuid.UUID) -> Any:
            return self.in_force

        async def judged_criteria(_session: Any, _submission: Any) -> Any:
            return self.judged

        async def load_task_context(
            _session: Any, _doc: uuid.UUID
        ) -> tuple[str, str, str]:
            return _TASK_CONTEXT

        async def course_language(_session: Any, _submission: Any) -> str:
            return "Ukrainian"

        monkeypatch.setattr(script, "door_languages", door_languages)
        monkeypatch.setattr(script, "load_in_force", load_in_force)
        monkeypatch.setattr(script, "judged_criteria", judged_criteria)
        monkeypatch.setattr(script, "load_task_context", load_task_context)
        monkeypatch.setattr(script, "course_language", course_language)

    async def record(self) -> script.Recorded:
        return await script.record(self.session, self.s3, self.submission.id)


def _expected_variables() -> dict[str, Any]:
    """What the stage renders for the same sources: the doors on the same bytes."""
    reading = read_text_work(
        filename=_FILENAME, file_bytes=_WORK, languages=["uk"], note_flags=()
    )
    shown = EvaluationInput(
        task_title=_TASK_CONTEXT[0],
        task_description=_TASK_CONTEXT[1],
        task_text=_TASK_CONTEXT[2],
        criteria=_CRITERIA,
        submission_text=reading.text,
        language="Ukrainian",
    )
    variables = render_context(shown, expected=judged_items(_CRITERIA), repeat=())
    loaded: dict[str, Any] = json.loads(json.dumps(variables, ensure_ascii=False))
    return loaded


class TestTheRecordedInput:
    async def test_the_first_requests_variables_from_the_stages_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world = _World(tmp_path)
        world.install(monkeypatch)

        recorded = await world.record()

        assert recorded.variables == _expected_variables()
        assert recorded.variables["items"] == ["c1", "c2.p1", "c2.p2"]
        assert recorded.variables["repeat"] == []
        assert "def solve(n):" in recorded.variables["submission_text"]
        assert (recorded.criteria_layer, recorded.criteria_source_id) == (
            "model",
            str(_IN_FORCE.source_id),
        )
        assert recorded.filename_was_temporary is False
        # The world's submission has no verdict rows: the list in force.
        assert recorded.criteria_from == script.FROM_IN_FORCE

    async def test_the_list_the_verdicts_were_judged_against_comes_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An author's edit since the run put another list in force; the
        # verdict rows still name the one the stage was handed.
        judged = CriteriaInForce(
            criteria=_CRITERIA, layer=CriteriaLayer.AUTHOR, source_id=uuid.uuid4()
        )
        edited = CriteriaInForce(
            criteria=(_criterion("c1", "must"),),
            layer=CriteriaLayer.AUTHOR,
            source_id=uuid.uuid4(),
        )
        world = _World(tmp_path, in_force=edited, judged=judged)
        world.install(monkeypatch)

        recorded = await world.record()

        assert recorded.variables == _expected_variables()
        assert (recorded.criteria_layer, recorded.criteria_source_id) == (
            "author",
            str(judged.source_id),
        )
        assert recorded.criteria_from == script.FROM_VERDICT_ROWS


class TestTheJudgedList:
    """``judged_criteria``: by the address of the rows, as the explanation's."""

    async def test_no_rows_no_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def no_rows(_session: Any, _submission: Any) -> list[Any]:
            return []

        monkeypatch.setattr(script, "verdict_rows", no_rows)

        assert await script.judged_criteria(MagicMock(), MagicMock()) is None

    async def test_rows_of_two_lists_are_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def two_lists(_session: Any, _submission: Any) -> list[Any]:
            return [
                SimpleNamespace(criteria_layer="model", criteria_source_id=uuid.uuid4())
                for _ in range(2)
            ]

        monkeypatch.setattr(script, "verdict_rows", two_lists)

        with pytest.raises(script.RecordRefusedError, match="2 lists"):
            await script.judged_criteria(MagicMock(), MagicMock())

    async def test_the_file_is_what_measure_determinism_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world = _World(tmp_path)
        world.install(monkeypatch)
        recorded = await world.record()
        out = tmp_path / "out" / "recorded.json"

        script.write_input(recorded, out)

        assert json.loads(out.read_text(encoding="utf-8")) == recorded.variables

    async def test_the_download_is_deleted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world = _World(tmp_path)
        world.install(monkeypatch)

        await world.record()

        assert not world.download.exists()


class TestAnArchive:
    """The file's name decides how the doors read its bytes: an archive."""

    async def test_read_as_an_archive_file_by_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("solve.py", "def solve(n):\n    return n * 2\n")
            zf.writestr("README.md", "Doubling, as asked in the task.\n")
        world = _World(tmp_path, original_filename="answers.zip")
        world.download = tmp_path / "download-9f8e.zip"
        world.s3.download_file = AsyncMock(
            side_effect=lambda _key: _write(world.download, archive.getvalue())
        )
        world.install(monkeypatch)

        recorded = await world.record()

        door = read_text_work(
            filename="answers.zip",
            file_bytes=archive.getvalue(),
            languages=["uk"],
            note_flags=(),
        )
        assert recorded.variables["submission_text"] == door.text
        assert "solve.py" in door.text
        assert "README.md" in door.text


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


class TestRefusals:
    async def test_no_stored_list_stops_before_any_download(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world = _World(tmp_path, in_force=None)
        world.install(monkeypatch)

        with pytest.raises(script.RecordRefusedError, match="no verdict rows"):
            await world.record()
        world.s3.download_file.assert_not_awaited()

    @pytest.mark.parametrize("task_type", ["project", "test", None])
    async def test_a_type_the_stage_does_not_serve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_type: str | None
    ) -> None:
        world = _World(tmp_path, task_type=task_type)  # type: ignore[arg-type]
        world.install(monkeypatch)

        with pytest.raises(script.RecordRefusedError, match="serves"):
            await world.record()
        world.s3.download_file.assert_not_awaited()

    async def test_no_submission(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world = _World(tmp_path)
        world.install(monkeypatch)
        world.session.get = AsyncMock(return_value=None)

        with pytest.raises(script.RecordRefusedError, match="no submission"):
            await world.record()


def _criteria_stage() -> Any:
    return model_stages(load_path_config(Path("config/submission_paths.yaml")))[
        script.STAGE
    ]


def _registry() -> Any:
    return load_registry(Path("config/external_services.yaml"))


class TestTheFirstRungsRequest:
    async def test_built_by_the_router_without_a_seed(self) -> None:
        provider, request = await script.first_rung_request(
            _expected_variables(), _criteria_stage(), _registry()
        )

        assert (provider, request.model) == ("dashscope", "qwen3.7-max")
        assert request.seed is None
        assert request.temperature == 0.0
        assert request.schema_mode is SchemaMode.STRICT
        assert request.max_tokens == 16384

    async def test_its_hash_is_the_measurements_unseeded_one(
        self, tmp_path: Path
    ) -> None:
        # The recorded file, fed to measure_determinism, is asked in its
        # ``no_seed`` mode with exactly the request this check hashes.
        variables = _expected_variables()
        provider, request = await script.first_rung_request(
            variables, _criteria_stage(), _registry()
        )

        class _Echo(LLMProvider):
            provider_name = "dashscope"

            def seed_supported(self, _request: LLMRequest) -> bool:
                return True

            async def complete(self, _request: LLMRequest) -> LLMResponse:
                return LLMResponse(content="{}", provider="dashscope", model_id="m")

        await measure_determinism.measure(
            _criteria_stage(), _Echo(), _registry(), variables, runs=1, out_dir=tmp_path
        )
        lines = (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
        by_mode = {json.loads(line)["mode"]: json.loads(line) for line in lines}

        assert by_mode["no_seed"]["input_hash"] == hash_attempt_input(
            request, provider=provider
        )
        assert by_mode["seed"]["input_hash"] != by_mode["no_seed"]["input_hash"]


def _row(
    input_hash: str | None,
    prompt_hash: str = "p",
    *,
    created_at: str = "2026-11-12T00:00:00+00:00",
    **columns: Any,
) -> script.RegisterRow:
    return script.RegisterRow(
        id=str(uuid.uuid4()),
        created_at=created_at,
        outcome="success",
        input_hash=input_hash,
        prompt_hash=prompt_hash,
        **columns,
    )


_SEED = 20261113


class TestWhatEachRowWasSentWith:
    """The rule of the module docstring, row by row."""

    def test_a_row_with_its_columns_says_it_itself(self) -> None:
        row = _row(
            "h",
            created_at="2026-11-01T00:00:00+00:00",
            request_kind="first",
            seed=7,
            temperature=0.3,
        )

        sent = script.sent_with(row, seed_supported=False)

        assert (sent.seed, sent.temperature, sent.rule) == (7, 0.3, "columns")

    def test_a_row_with_columns_and_no_seed_sent_none(self) -> None:
        row = _row("h", request_kind="first", seed=None, temperature=0.0)

        sent = script.sent_with(row, seed_supported=True)

        assert (sent.seed, sent.temperature) == (None, 0.0)

    @pytest.mark.parametrize(
        ("created_at", "supported", "expected"),
        [
            ("2026-11-13T20:23:20+00:00", True, (None, "dated_before_seed")),
            ("2026-11-13T20:23:21+00:00", True, (_SEED, "dated_since_seed")),
            ("2026-11-14T00:00:00+01:00", True, (_SEED, "dated_since_seed")),
            ("2026-11-15T00:00:00+00:00", False, (None, "dated_since_seed")),
        ],
        ids=["a-second-before", "at-the-merge", "after-in-another-zone", "no-seed"],
    )
    def test_an_older_row_is_dated(
        self,
        created_at: str,
        supported: bool,
        expected: tuple[int | None, str],
    ) -> None:
        sent = script.sent_with(
            _row("h", created_at=created_at), seed_supported=supported
        )

        assert (sent.seed, sent.rule) == expected
        assert sent.temperature == 0.0


class TestTheComparison:
    def _recorded(
        self, *, temporary: bool = False, criteria_from: str = "verdict_rows"
    ) -> script.Recorded:
        return script.Recorded(
            variables={},
            criteria_layer="model",
            criteria_source_id="s",
            filename_was_temporary=temporary,
            criteria_from=criteria_from,
        )

    def _request(self) -> LLMRequest:
        return LLMRequest(prompt="p", model="qwen3.7-max")

    def _hash(self, **update: Any) -> str:
        return hash_attempt_input(
            self._request().model_copy(update=update), provider="dashscope"
        )

    def test_a_row_with_the_same_hash_is_matched(self) -> None:
        mine = self._hash()
        check = script.compare(
            self._request(),
            provider="dashscope",
            prompt_hash="p",
            rows=[_row("other"), _row(mine)],
            recorded=self._recorded(),
            seed_supported=True,
        )

        assert check.matched is True
        assert [row.matched for row in check.rows] == [False, True]
        assert check.rows[1].rebuilt_hash == mine
        assert check.notes == []

    def test_each_row_is_hashed_with_its_own_seed(self) -> None:
        # The same input before the seed, since it, and from the columns.
        before = _row(self._hash())
        since = _row(
            self._hash(seed=_SEED, temperature=0.0),
            created_at="2026-11-14T00:00:00+00:00",
        )
        columns = _row(
            self._hash(seed=_SEED, temperature=0.0),
            created_at="2026-11-17T00:00:00+00:00",
            request_kind="first",
            seed=_SEED,
            temperature=0.0,
        )

        check = script.compare(
            self._request(),
            provider="dashscope",
            prompt_hash="p",
            rows=[before, since, columns],
            recorded=self._recorded(),
            seed_supported=True,
        )

        assert [row.matched for row in check.rows] == [True, True, True]
        assert [row.sent_rule for row in check.rows] == [
            "dated_before_seed",
            "dated_since_seed",
            "columns",
        ]

    def test_an_unseeded_hash_does_not_match_a_seeded_row(self) -> None:
        check = script.compare(
            self._request(),
            provider="dashscope",
            prompt_hash="p",
            rows=[
                _row(self._hash(), request_kind="first", seed=_SEED, temperature=0.0)
            ],
            recorded=self._recorded(),
            seed_supported=True,
        )

        assert check.matched is False

    def test_no_row_with_it_is_not(self) -> None:
        check = script.compare(
            self._request(),
            provider="dashscope",
            prompt_hash="p",
            rows=[_row("other"), _row(None)],
            recorded=self._recorded(),
            seed_supported=True,
        )

        assert check.matched is False

    def test_the_notes_say_why_it_cannot_match(self) -> None:
        empty = script.compare(
            self._request(),
            provider="dashscope",
            prompt_hash="p",
            rows=[],
            recorded=self._recorded(temporary=True, criteria_from="in_force"),
            seed_supported=True,
        )
        moved = script.compare(
            self._request(),
            provider="dashscope",
            prompt_hash="today",
            rows=[_row("other", prompt_hash="then")],
            recorded=self._recorded(),
            seed_supported=True,
        )

        assert empty.matched is False
        assert any("no attempt row" in n for n in empty.notes)
        assert any("temporary name" in n for n in empty.notes)
        assert any("no verdict rows" in n for n in empty.notes)
        assert any("prompt moved" in n for n in moved.notes)


def test_the_first_rungs_connector_takes_the_seed() -> None:
    request = LLMRequest(prompt="p", model="qwen3.7-max")

    assert script.seed_supported("dashscope", request) is True
    assert (
        script.seed_supported(
            "deepseek", request.model_copy(update={"model": "deepseek-flash"})
        )
        is False
    )


def test_the_check_is_written_beside_the_input() -> None:
    assert script.check_path(Path("out/recorded.json")) == Path(
        "out/recorded.check.json"
    )


def test_the_submission_must_be_a_uuid() -> None:
    with pytest.raises(SystemExit):
        script._parse(["--submission", "42", "--out", "o.json"])
