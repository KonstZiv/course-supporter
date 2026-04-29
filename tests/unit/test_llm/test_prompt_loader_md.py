"""Tests for the KD16 markdown prompt loader."""

from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from jinja2 import UndefinedError

from course_supporter.llm.error_categories import InvalidPromptError
from course_supporter.llm.prompt_loader_md import (
    StagePrompt,
    _warned_unknown_roles,
    load_prompt,
)


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


# ── Parsing only (load_prompt) ─────────────────────────────────────


class TestLoadPrompt:
    def test_parses_three_sections(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "p.md",
            "## System\nYou are helpful.\n\n## User\nHi {{ name }}\n\n"
            "## Assistant\nHello.\n",
        )

        prompt = load_prompt("p.md", base_path=tmp_path)

        assert prompt.system == "You are helpful."
        assert prompt.user == "Hi {{ name }}"
        assert prompt.assistant == "Hello."

    def test_absent_section_is_none(self, tmp_path: Path) -> None:
        _write(tmp_path / "p.md", "## User\nJust a user prompt\n")

        prompt = load_prompt("p.md", base_path=tmp_path)

        assert prompt.system is None
        assert prompt.user == "Just a user prompt"
        assert prompt.assistant is None

    def test_header_only_no_body_yields_empty_string(
        self,
        tmp_path: Path,
    ) -> None:
        _write(tmp_path / "p.md", "## System\n\n## User\nask\n")

        prompt = load_prompt("p.md", base_path=tmp_path)

        # Header present, body empty -> empty string (NOT None).
        assert prompt.system == ""
        assert prompt.user == "ask"

    def test_role_names_are_case_insensitive(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "p.md",
            "## SYSTEM\nsys\n\n## User\nu\n\n## assistant\na\n",
        )

        prompt = load_prompt("p.md", base_path=tmp_path)

        assert prompt.system == "sys"
        assert prompt.user == "u"
        assert prompt.assistant == "a"

    def test_unknown_role_is_silently_dropped(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "p.md",
            "## Examples\nfoo bar baz\n\n## User\nactual prompt\n",
        )

        prompt = load_prompt("p.md", base_path=tmp_path)

        assert prompt.user == "actual prompt"
        assert prompt.system is None
        assert prompt.assistant is None

    def test_whitespace_prologue_allowed(self, tmp_path: Path) -> None:
        _write(tmp_path / "p.md", "\n\n   \n## User\nhello\n")

        prompt = load_prompt("p.md", base_path=tmp_path)

        assert prompt.user == "hello"

    def test_content_before_first_header_rejected(
        self,
        tmp_path: Path,
    ) -> None:
        _write(tmp_path / "p.md", "Some preamble.\n\n## User\nhi\n")

        with pytest.raises(InvalidPromptError) as exc_info:
            load_prompt("p.md", base_path=tmp_path)

        assert "content before first" in str(exc_info.value)
        assert exc_info.value.prompt_ref == "p.md"

    def test_no_recognised_sections_rejected(self, tmp_path: Path) -> None:
        _write(tmp_path / "p.md", "## Examples\nfoo\n\n## Notes\nbar\n")

        with pytest.raises(InvalidPromptError) as exc_info:
            load_prompt("p.md", base_path=tmp_path)

        assert "no recognised role sections" in str(exc_info.value)

    def test_empty_file_rejected(self, tmp_path: Path) -> None:
        _write(tmp_path / "p.md", "")

        with pytest.raises(InvalidPromptError):
            load_prompt("p.md", base_path=tmp_path)

    def test_missing_file_raises_filenotfound(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_prompt("missing.md", base_path=tmp_path)

    def test_load_does_not_render_jinja2(self, tmp_path: Path) -> None:
        # Even if a placeholder is present, load should preserve it
        # raw -- rendering happens in StagePrompt.render().
        _write(
            tmp_path / "p.md",
            "## User\nHello {{ unrendered_var }}!\n",
        )

        prompt = load_prompt("p.md", base_path=tmp_path)

        assert prompt.user == "Hello {{ unrendered_var }}!"


# ── Rendering only (StagePrompt.render) ────────────────────────────


class TestStagePromptRender:
    def test_renders_known_variables(self) -> None:
        prompt = StagePrompt(
            system="You speak {{ language }}.",
            user="Tell me about {{ subject }}.",
        )

        rendered = prompt.render(language="Ukrainian", subject="Python")

        assert rendered.system == "You speak Ukrainian."
        assert rendered.user == "Tell me about Python."
        assert rendered.assistant is None

    def test_missing_variable_raises_undefined_error(self) -> None:
        prompt = StagePrompt(user="Hi {{ name }}")

        with pytest.raises(UndefinedError):
            prompt.render()

    def test_none_fields_remain_none(self) -> None:
        prompt = StagePrompt(user="hello")

        rendered = prompt.render()

        assert rendered.system is None
        assert rendered.assistant is None
        assert rendered.user == "hello"

    def test_empty_string_fields_remain_empty(self) -> None:
        prompt = StagePrompt(system="", user="hi")

        rendered = prompt.render()

        # Header-present-but-empty stays "" through render -- caller
        # can still distinguish absent (None) vs present-empty ("").
        assert rendered.system == ""
        assert rendered.user == "hi"

    def test_render_returns_new_instance(self) -> None:
        prompt = StagePrompt(user="static text")

        rendered = prompt.render()

        assert rendered is not prompt
        # Frozen dataclasses with the same field values compare equal.
        assert rendered == prompt

    def test_extra_context_keys_are_ignored(self) -> None:
        prompt = StagePrompt(user="Hi {{ name }}")

        rendered = prompt.render(name="Bob", unused="xxx")

        assert rendered.user == "Hi Bob"


# ── Load + render integration ──────────────────────────────────────


class TestEndToEnd:
    def test_load_then_render(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "p.md",
            "## System\nYou speak {{ language }}.\n\n## User\nExplain {{ topic }}.\n",
        )

        prompt = load_prompt("p.md", base_path=tmp_path).render(
            language="Ukrainian", topic="recursion"
        )

        assert prompt.system == "You speak Ukrainian."
        assert prompt.user == "Explain recursion."

    def test_real_example_prompt_renders(self) -> None:
        # Smoke test against the checked-in prompts/example/v1.md.
        # CWD is the backend repo root in CI.
        prompt = load_prompt("prompts/example/v1.md").render(
            subject="Python",
            language="Ukrainian",
            question="What is recursion?",
        )

        assert prompt.system is not None
        assert "Python" in prompt.system
        assert "Ukrainian" in prompt.system
        assert prompt.user == "What is recursion?"


# ── Unknown-role WARNING + dedup (commit (g) review fix) ───────────


@pytest.fixture(autouse=False)
def _reset_unknown_role_dedup() -> Iterator[None]:
    """Clear the process-local dedup set so tests don't leak state."""
    _warned_unknown_roles.clear()
    yield
    _warned_unknown_roles.clear()


class TestUnknownRoleWarning:
    def test_unknown_role_logs_warning(
        self,
        tmp_path: Path,
        _reset_unknown_role_dedup: None,
    ) -> None:
        _write(
            tmp_path / "p.md",
            "## System\nsys body\n\n## Examples\nfoo\n\n## User\nu body\n",
        )

        with structlog.testing.capture_logs() as captured:
            prompt = load_prompt("p.md", base_path=tmp_path)

        # Recognised sections still parsed correctly.
        assert prompt.system == "sys body"
        assert prompt.user == "u body"

        # Exactly one WARNING emitted for the unknown role.
        warnings = [
            r for r in captured if r.get("event") == "prompt_loader_unknown_role"
        ]
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["unknown_role"] == "examples"
        assert warnings[0]["prompt_ref"] == "p.md"
        assert warnings[0]["recognised_roles"] == [
            "assistant",
            "system",
            "user",
        ]

    def test_unknown_role_warning_deduplicated(
        self,
        tmp_path: Path,
        _reset_unknown_role_dedup: None,
    ) -> None:
        _write(
            tmp_path / "p.md",
            "## System\nsys\n\n## Examples\nfoo\n\n## User\nu\n",
        )

        with structlog.testing.capture_logs() as captured:
            load_prompt("p.md", base_path=tmp_path)
            load_prompt("p.md", base_path=tmp_path)
            load_prompt("p.md", base_path=tmp_path)

        warnings = [
            r for r in captured if r.get("event") == "prompt_loader_unknown_role"
        ]
        # Three loads of the same file -> still exactly one warning.
        assert len(warnings) == 1

    def test_different_unknown_roles_each_warn_once(
        self,
        tmp_path: Path,
        _reset_unknown_role_dedup: None,
    ) -> None:
        _write(
            tmp_path / "p.md",
            "## System\ns\n\n## Examples\nfoo\n\n## Notes\nbar\n\n## User\nu\n",
        )

        with structlog.testing.capture_logs() as captured:
            load_prompt("p.md", base_path=tmp_path)
            load_prompt("p.md", base_path=tmp_path)

        warnings = [
            r for r in captured if r.get("event") == "prompt_loader_unknown_role"
        ]
        # Two distinct unknown roles -> two warnings even across two loads.
        roles = sorted(w["unknown_role"] for w in warnings)
        assert roles == ["examples", "notes"]
