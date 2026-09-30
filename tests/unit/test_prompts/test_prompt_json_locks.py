"""Locks on the rule "JSON reaches the model as a person reads it".

Every place that serialises data into a prompt goes through
:func:`~course_supporter.llm.prompt_json.prompt_json`: letters, the apostrophe
and ``&`` stay as they are, only ``<`` and ``>`` are escaped (``\\u003c`` /
``\\u003e``) so the data cannot close a tag around it.

* Static lock: no prompt template uses Jinja2's stock ``tojson``, which
  escapes every non-ASCII letter — unless it is allowed below with a reason.
* Dynamic lock: the function itself, and each of the seven places that put
  JSON into a prompt, driven through the code that builds the prompt (the
  agent or pipeline step, then the stage's own template rendered from the
  render context the step handed to the router — what ``StageRouter`` does).
  Each serialised fragment must equal ``json.dumps(ensure_ascii=False, ...)``
  with that place's parameters, ``<`` and ``>`` replaced, and nothing else
  escaped.
"""

from __future__ import annotations

import json
import re
import uuid
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from course_supporter.agents.criteria_decomposer import CriteriaDecomposerAgent
from course_supporter.agents.methodist import MethodistAgent, OwnDocument
from course_supporter.ingestion.audio import AudioProcessor
from course_supporter.ingestion.schemas import DocumentSegmentDraft
from course_supporter.ingestion.video_pipeline import steps
from course_supporter.ingestion.video_pipeline.schemas import SttResult, SttWord
from course_supporter.llm.ladder_config import load_ladder_config
from course_supporter.llm.prompt_json import prompt_json
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.llm.stage_router import StageResult
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)
from course_supporter.service_logging import set_job_from_arq
from course_supporter.stt.schemas import STTResult, STTSegment, STTWord

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS_DIR = _REPO_ROOT / "prompts"

_COMPACT = (",", ":")

# Cyrillic, the Ukrainian apostrophe, "&", "<" and ">" — no control characters
# (json.dumps escapes those whatever the rule says).
_HOSTILE = [
    "Змінна",
    "Об'єкт класу",
    "м'який знак",
    "a & b",
    "x < y > z",
    "кінець </node_canonical> тегу",
]
_ANGLES_PER_LIST = sum(s.count("<") + s.count(">") for s in _HOSTILE)


def _expected(
    value: Any,
    *,
    sort_keys: bool = False,
    separators: tuple[str, str] | None = None,
) -> str:
    """The rule written out: plain json.dumps, then only < and > replaced."""
    text = json.dumps(
        value, ensure_ascii=False, sort_keys=sort_keys, separators=separators
    )
    return text.replace("<", "\\u003c").replace(">", "\\u003e")


def _angles(value: Any) -> int:
    text = json.dumps(value, ensure_ascii=False)
    return text.count("<") + text.count(">")


def _assert_fragment(fragment: str, value: Any) -> None:
    """Letters, apostrophe and & verbatim; one escape per < or >; no raw < or >."""
    assert fragment.count("\\u") == _angles(value)
    assert "<" not in fragment
    assert ">" not in fragment
    assert "Об'єкт" in fragment or "об'єкт" in fragment
    assert "&" in fragment
    assert re.search(r"[А-яІіЇїЄєҐґ]", fragment)


@cache
def _prompt_ref(stage_name: str) -> str:
    return load_ladder_config(_REPO_ROOT / "config").get_stage(stage_name).prompt_ref


def _render(stage_name: str, render_context: dict[str, Any]) -> str:
    """Render the stage's template as StageRouter does; System + User text."""
    template = load_prompt(_prompt_ref(stage_name), base_path=_REPO_ROOT)
    rendered = template.render(**render_context)
    return (rendered.system or "") + "\n" + (rendered.user or "")


class _CapturingRouter:
    """StageRouter double: records the render context, feeds a canned reply."""

    def __init__(self, reply: Any) -> None:
        self._reply = reply
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute_for_stage(
        self,
        stage_name: str,
        *,
        response_validator: Any = None,
        expects_json: bool = False,
        contents: Any = None,
        **render_context: Any,
    ) -> StageResult:
        del expects_json, contents
        self.calls.append((stage_name, render_context))
        content = self._reply(render_context) if callable(self._reply) else self._reply
        if response_validator is not None:
            response_validator(content)
        return StageResult(
            content=content,
            provider_used="mock",
            model_used="mock-model",
            attempt_count=1,
        )

    async def execute_stage(
        self,
        stage: Any,
        stage_name: str,
        /,
        *,
        response_validator: Any = None,
        expects_json: bool = False,
        contents: Any = None,
        stop_on_output_ceiling: bool = False,
        money_ceiling_usd: float | None = None,
        **render_context: Any,
    ) -> StageResult:
        """The entry of a caller holding its own stage — the same capture."""
        del stage, stop_on_output_ceiling, money_ceiling_usd
        return await self.execute_for_stage(
            stage_name,
            response_validator=response_validator,
            expects_json=expects_json,
            contents=contents,
            **render_context,
        )


# ── Static lock ─────────────────────────────────────────────────────

# Templates allowed to keep Jinja2's stock ``tojson``: {path under prompts/:
# reason}. Every entry needs a reason a reviewer can check.
_STOCK_TOJSON_ALLOWED: dict[str, str] = {}

_STOCK_TOJSON = re.compile(r"\|\s*tojson\b")


def test_no_prompt_uses_stock_tojson() -> None:
    users = {
        path.relative_to(_PROMPTS_DIR).as_posix()
        for path in sorted(_PROMPTS_DIR.rglob("*.md"))
        if _STOCK_TOJSON.search(path.read_text(encoding="utf-8"))
    }

    assert users == set(_STOCK_TOJSON_ALLOWED), (
        "Jinja2's stock tojson escapes every non-ASCII letter as \\uXXXX; "
        "use tojson_unicode, or allow the template with a reason"
    )
    assert all(reason.strip() for reason in _STOCK_TOJSON_ALLOWED.values())


def test_static_lock_sees_stock_tojson() -> None:
    assert _STOCK_TOJSON.search("{{ items | tojson }}")
    assert _STOCK_TOJSON.search("{{ items|tojson(2) }}")
    assert not _STOCK_TOJSON.search("{{ items | tojson_unicode }}")


# ── Dynamic lock: the function ──────────────────────────────────────


@pytest.mark.parametrize(
    ("sort_keys", "separators"),
    [
        pytest.param(True, None, id="methodist-filter"),
        pytest.param(False, _COMPACT, id="ingestion-compact"),
    ],
)
def test_prompt_json_follows_the_rule(
    sort_keys: bool, separators: tuple[str, str] | None
) -> None:
    value = {"я": _HOSTILE, "a<b>": {"б": "об'єкт & <тег>", "а": 1}}

    fragment = prompt_json(value, sort_keys=sort_keys, separators=separators)

    assert fragment == _expected(value, sort_keys=sort_keys, separators=separators)
    _assert_fragment(fragment, value)
    assert json.loads(fragment) == value


# ── Dynamic lock: Methodist (tojson_unicode in the templates) ────────


def _node_raw(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "title": "Заняття 3",
        "description": "Функції.",
        "learning_objectives": _HOSTILE,
        "knowledge": [],
        "skills": [],
        "success_criteria": [],
        "assessment_approach": "Задачі.",
        "teaching_approach": "Від прикладу до правила.",
        "key_activities": _HOSTILE,
        "common_mistakes": [],
        "compressed_summary": "Стислий опис.",
        "methodist_observations": [],
        "main_concepts": _HOSTILE,
        "secondary_concepts": _HOSTILE,
        "own_documents_count": 0,
        "own_chars_count": 0,
        "cumulative_documents_count": 0,
        "cumulative_chars_count": 0,
        "enclosing_context": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _MethodistAgent(MethodistAgent):
    """The real agent with its three DB readers replaced by fixtures."""

    def __init__(self, router: _CapturingRouter) -> None:
        super().__init__(session=AsyncMock(), stage_router=router)  # type: ignore[arg-type]

    async def _fetch_own_documents(self, course_node_id: Any) -> list[OwnDocument]:
        del course_node_id
        return [
            OwnDocument(
                title="Лекція",
                description="Опис.",
                main_concepts=_HOSTILE,
                secondary_concepts=_HOSTILE,
                content_char_count=100,
                material_role="educational",
                task_type=None,
            )
        ]

    async def _fetch_children_raws(self, course_node_id: Any) -> list[dict[str, Any]]:
        del course_node_id
        return []

    async def _resolve_course_context(self, node: Any) -> tuple[str, str | None]:
        del node
        return "Курс Python", "Ukrainian"


_BOTTOMUP_REPLY = json.dumps(
    {
        "title": "Функції",
        "description": "Що таке функція.",
        "learning_objectives": [],
        "knowledge": [],
        "skills": [],
        "success_criteria": [],
        "assessment_approach": "Задачі.",
        "teaching_approach": "Практика.",
        "key_activities": [],
        "common_mistakes": [],
        "compressed_summary": "Вузол про функції. " * 60,
        "methodist_observations": [],
    },
    ensure_ascii=False,
)
_TOPDOWN_REPLY = json.dumps(
    {"enclosing_context": "Контекст вузла в курсі. " * 50, "observations": []},
    ensure_ascii=False,
)


async def test_methodist_bottomup_prompt() -> None:
    router = _CapturingRouter(_BOTTOMUP_REPLY)
    node = SimpleNamespace(id=uuid.uuid4(), title="Заняття 3", parent_id=None)

    await _MethodistAgent(router).generate_bottomup(node, _node_raw())  # type: ignore[arg-type]

    [(stage, context)] = router.calls
    text = _render(stage, context)
    fragment = _expected(_HOSTILE, sort_keys=True)
    _assert_fragment(fragment, _HOSTILE)
    assert f"  main_concepts: {fragment}\n" in text
    assert f"  secondary_concepts: {fragment}\n" in text
    assert text.count("\\u") == 2 * _ANGLES_PER_LIST


async def test_methodist_topdown_prompt() -> None:
    router = _CapturingRouter(_TOPDOWN_REPLY)
    node = SimpleNamespace(id=uuid.uuid4(), title="Заняття 3", parent_id=uuid.uuid4())

    await _MethodistAgent(router).generate_topdown(  # type: ignore[arg-type]
        node, _node_raw(), _node_raw(title="Курс Python")
    )

    [(stage, context)] = router.calls
    text = _render(stage, context)
    fragment = _expected(_HOSTILE, sort_keys=True)
    _assert_fragment(fragment, _HOSTILE)
    for field in ("learning_objectives", "main_concepts", "secondary_concepts"):
        assert text.count(f"\n{field}: {fragment}\n") == 2  # node and parent
    assert text.count(f"\nkey_activities: {fragment}\n") == 1
    assert text.count("\\u") == 7 * _ANGLES_PER_LIST


# ── Dynamic lock: audio and video Pass 2a / 2c (prompt_json in Python) ──


def _segments_reply(render_context: dict[str, Any]) -> str:
    """A valid, balanced Pass 2a reply over the whole word stream."""
    n = render_context["words_count"]
    bounds = [round(i * n / 3) for i in range(4)]
    return json.dumps(
        {
            "title": "Тема",
            "description": "Опис.",
            "segments": [
                {
                    "start_word_idx": bounds[i],
                    "end_word_idx": bounds[i + 1],
                    "title": f"Частина {i}",
                    "description": "Опис частини.",
                    "main_concepts": [],
                    "secondary_concepts": [],
                    "noisy": False,
                    "subsegments": [],
                }
                for i in range(3)
            ],
        },
        ensure_ascii=False,
    )


def _words_fragment(text: str) -> str:
    """The words_json line of a rendered 2a prompt (the one line opening '[{')."""
    [line] = [ln for ln in text.splitlines() if ln.startswith('[{"text":')]
    return line


async def test_audio_pass_2a_prompt() -> None:
    words = [
        STTWord(text=t, start_sec=i * 0.5, end_sec=i * 0.5 + 0.4)
        for i, t in enumerate(_HOSTILE)
    ]
    stt = STTResult(
        text=" ".join(_HOSTILE),
        segments=[STTSegment(start_sec=0.0, end_sec=3.0, text=" ".join(_HOSTILE))],
        words=words,
        provider="mock-scribe",
        model_id="mock-scribe-v2",
    )
    stt_router = AsyncMock()
    stt_router.transcribe.return_value = stt
    store: dict[str, str] = {}
    redis = AsyncMock()
    redis.set.side_effect = lambda key, value, ex=None: store.__setitem__(key, value)
    redis.get.side_effect = store.get
    proc = AudioProcessor(stt_router=stt_router, redis=redis)
    set_job_from_arq(uuid.uuid4())
    doc_row = SimpleNamespace(
        source_type=SourceType.AUDIO, source_url="/tmp/a.mp3", filename="a.mp3"
    )
    doc = await proc.process_raw(doc_row)  # type: ignore[arg-type]
    router = _CapturingRouter(_segments_reply)

    await proc.process_macro(doc, router)  # type: ignore[arg-type]

    [(stage, context)] = router.calls
    text = _render(stage, context)
    fragment = _words_fragment(text)
    value = json.loads(fragment)
    assert [list(w) for w in value] == [["text", "start", "end", "logprob"]] * len(
        _HOSTILE
    )
    assert [w["text"] for w in value] == _HOSTILE
    assert fragment == _expected(value, separators=_COMPACT)
    _assert_fragment(fragment, value)
    assert text.count("\\u") == _ANGLES_PER_LIST


async def test_video_pass_2a_prompt() -> None:
    words = [
        SttWord(text=t, start_ms=i * 500, end_ms=i * 500 + 400)
        for i, t in enumerate(_HOSTILE)
    ]
    stt = SttResult(language="uk", duration_ms=3_000, words=words, pauses=[])
    doc = SourceDocument(
        source_type=SourceType.VIDEO,
        source_url="s3://b/v.mp4",
        title="v",
        chunks=[
            ContentChunk(
                chunk_type=ChunkType.TRANSCRIPT, text=" ".join(_HOSTILE), index=0
            )
        ],
    )
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=stt.model_dump_json())
    router = _CapturingRouter(_segments_reply)

    with patch(
        "course_supporter.ingestion.video_pipeline.steps.get_current_job_id",
        return_value=uuid.uuid4(),
    ):
        await steps.step_5_pass2a_mapping(doc, redis=redis, stage_router=router)  # type: ignore[arg-type]

    [(stage, context)] = router.calls
    text = _render(stage, context)
    fragment = _words_fragment(text)
    value = json.loads(fragment)
    assert [list(w) for w in value] == [["text", "start", "end"]] * len(_HOSTILE)
    assert [w["text"] for w in value] == _HOSTILE
    assert fragment == _expected(value, separators=_COMPACT)
    _assert_fragment(fragment, value)
    assert text.count("\\u") == _ANGLES_PER_LIST


def _noisy_segment() -> DocumentSegmentDraft:
    return DocumentSegmentDraft(
        order=0,
        start_pos=0,
        end_pos=10,
        description="Опис.",
        main_concepts=_HOSTILE,
        secondary_concepts=_HOSTILE,
        content="е-е сирий шматок транскрипту",
        noisy=True,
    )


def _assert_denoise_prompt(text: str) -> None:
    fragment = _expected(_HOSTILE, separators=_COMPACT)
    _assert_fragment(fragment, _HOSTILE)
    assert f"<main_concepts>{fragment}</main_concepts>" in text
    assert f"<secondary_concepts>{fragment}</secondary_concepts>" in text
    assert text.count("\\u") == 2 * _ANGLES_PER_LIST


async def test_audio_pass_2c_prompt() -> None:
    proc = AudioProcessor(stt_router=AsyncMock(), redis=AsyncMock())
    router = _CapturingRouter("очищений шматок")

    await proc._denoise_segment(_noisy_segment(), router)  # type: ignore[arg-type]

    [(stage, context)] = router.calls
    _assert_denoise_prompt(_render(stage, context))


async def test_video_pass_2c_prompt() -> None:
    router = _CapturingRouter("очищений шматок")

    await steps.step_7_pass2c_cleanup([_noisy_segment()], stage_router=router)  # type: ignore[arg-type]

    [(stage, context)] = router.calls
    _assert_denoise_prompt(_render(stage, context))


# ── Dynamic lock: criteria decomposition v2 (tojson_unicode in the template) ──

_CRITERIA_REPLY = json.dumps(
    {
        "criteria": [
            {
                "text": "Є базовий випадок.",
                "evidence": "Явне повернення для найменшого входу.",
                "weight": "must",
                "check_method": "model_verdict",
            }
        ]
    },
    ensure_ascii=False,
)


async def test_criteria_composition_prompt() -> None:
    router = _CapturingRouter(_CRITERIA_REPLY)

    await CriteriaDecomposerAgent(router).compose(  # type: ignore[arg-type]
        task_title="Факторіал",
        task_description="Рекурсія.",
        task_text="Напишіть рекурсивну функцію.",
        task_type="task",
        language="Ukrainian",
        node_description="Рекурсія в Python.",
        node_concepts=_HOSTILE,
        root_concepts=_HOSTILE,
    )

    [(stage, context)] = router.calls
    assert stage == "criteria_decomposition"
    text = _render(stage, context)
    fragment = _expected(_HOSTILE, sort_keys=True)
    _assert_fragment(fragment, _HOSTILE)
    assert f"<node_concepts>\n{fragment}\n</node_concepts>" in text
    assert f"<course_concepts>\n{fragment}\n</course_concepts>" in text
    assert text.count("\\u") == 2 * _ANGLES_PER_LIST
