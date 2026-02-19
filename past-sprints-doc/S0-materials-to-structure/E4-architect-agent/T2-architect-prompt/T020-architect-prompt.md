# 📋 S1-020: System Prompt v1 + Prompt Loader

## Мета

Створити system prompt для ArchitectAgent у YAML-файлі `prompts/architect/v1.yaml` та утиліти для його завантаження. Промпт інструктує LLM як аналізувати CourseContext і генерувати CourseStructure. Версіонування в YAML дозволяє A/B тестування різних промптів.

## Контекст

Друга задача Epic 4. Блокує S1-021 (ArchitectAgent використовує prompt). Залежить від S1-019 (CourseStructure schema описана в промпті). Файл `prompts/architect/v1.yaml` вже існує як stub (TODO) — потрібно замінити на реальний промпт.

---

## Acceptance Criteria

- [ ] `prompts/architect/v1.yaml` — YAML з `system_prompt` та `user_prompt_template`
- [ ] System prompt описує роль (course architect), output format (CourseStructure JSON), правила генерації
- [ ] User prompt template містить `{context}` placeholder для CourseContext
- [ ] `agents/prompt_loader.py` — `load_prompt(path) -> dict`, `format_user_prompt(template, context) -> str`
- [ ] `load_prompt` raises `FileNotFoundError` для неіснуючого файлу
- [ ] `load_prompt` raises `KeyError` для YAML без обов'язкових ключів
- [ ] ~8 unit-тестів, всі зелені
- [ ] `make check` проходить

---

## YAML-файл промпту

### prompts/architect/v1.yaml

```yaml
version: "v1"

system_prompt: |
  You are an expert Course Architect. Your task is to analyze course materials
  and generate a well-structured course program.

  ## Your Role
  - Analyze transcripts, slides, text documents, and web resources
  - Identify logical modules (major topics/sections)
  - Break modules into lessons (focused learning units)
  - Extract key concepts with definitions, examples, and cross-references
  - Design practical exercises with grading criteria

  ## Output Format
  Return a valid JSON object conforming to the CourseStructure schema:
  - title: descriptive course title
  - description: 2-3 sentence course overview
  - modules: list of modules, each containing lessons

  ## Rules
  1. Every lesson MUST have at least one concept
  2. Concepts MUST include cross-references to source materials:
     - timecodes (from video transcripts, format "HH:MM:SS")
     - slide_references (slide numbers from presentations)
     - web_references (URLs from web sources)
  3. Exercise difficulty_level: 1 (trivial) to 5 (advanced)
  4. Module and lesson order should follow the natural learning progression
  5. Use the original language of the source materials for titles and content
  6. Keep definitions concise but complete (1-3 sentences)
  7. Include 1-3 examples per concept where possible

  ## Quality Criteria
  - Comprehensive coverage of all source material topics
  - Logical progression from basic to advanced concepts
  - Cross-references that connect related content across sources
  - Exercises that reinforce key concepts

user_prompt_template: |
  Analyze the following course materials and generate a structured course program.

  ## Course Materials

  {context}

  ## Instructions
  Based on the materials above, generate a complete CourseStructure with:
  - A descriptive title and overview
  - Logical modules grouping related topics
  - Lessons within each module
  - Concept cards with definitions, examples, and cross-references
  - Practical exercises with difficulty levels

  Return ONLY valid JSON conforming to the CourseStructure schema.
```

---

## Prompt Loader

### src/course_supporter/agents/prompt_loader.py

```python
"""Prompt template loading and formatting utilities."""

from pathlib import Path
from typing import Any

import yaml


def load_prompt(path: str | Path) -> dict[str, Any]:
    """Load prompt template from YAML file.

    Args:
        path: Path to the YAML prompt file.

    Returns:
        Dict with at least 'system_prompt' and 'user_prompt_template' keys.

    Raises:
        FileNotFoundError: If the prompt file does not exist.
        KeyError: If required keys are missing from YAML.
    """
    prompt_path = Path(path)
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")

    with prompt_path.open() as f:
        data = yaml.safe_load(f)

    required_keys = {"system_prompt", "user_prompt_template"}
    missing = required_keys - set(data.keys())
    if missing:
        raise KeyError(f"Missing required keys in prompt file: {missing}")

    return dict(data)


def format_user_prompt(template: str, context: str) -> str:
    """Format user prompt template with context.

    Args:
        template: Prompt template with {context} placeholder.
        context: Serialized CourseContext to inject.

    Returns:
        Formatted prompt string.
    """
    return template.format(context=context)
```

### src/course_supporter/agents/__init__.py

```python
"""Agents for course structure generation."""

from course_supporter.agents.prompt_loader import format_user_prompt, load_prompt

__all__ = [
    "format_user_prompt",
    "load_prompt",
]
```

---

## Тести

### tests/unit/test_prompt_loader.py

```python
"""Tests for prompt loading and formatting utilities."""

import pytest
import yaml

from course_supporter.agents.prompt_loader import format_user_prompt, load_prompt


@pytest.fixture()
def valid_prompt_file(tmp_path: "Path") -> "Path":
    """Create a valid prompt YAML file."""
    data = {
        "version": "v1",
        "system_prompt": "You are a course architect.",
        "user_prompt_template": "Materials:\n{context}\nGenerate structure.",
    }
    path = tmp_path / "prompt.yaml"
    path.write_text(yaml.dump(data))
    return path


class TestLoadPrompt:
    def test_load_valid_prompt(self, valid_prompt_file: "Path") -> None:
        """Loads YAML and returns dict with required keys."""
        data = load_prompt(valid_prompt_file)
        assert "system_prompt" in data
        assert "user_prompt_template" in data
        assert data["version"] == "v1"

    def test_load_missing_file(self, tmp_path: "Path") -> None:
        """Raises FileNotFoundError for non-existent file."""
        with pytest.raises(FileNotFoundError):
            load_prompt(tmp_path / "nonexistent.yaml")

    def test_load_missing_system_prompt(self, tmp_path: "Path") -> None:
        """Raises KeyError when system_prompt is missing."""
        path = tmp_path / "bad.yaml"
        path.write_text(yaml.dump({"user_prompt_template": "test"}))
        with pytest.raises(KeyError, match="system_prompt"):
            load_prompt(path)

    def test_load_missing_user_template(self, tmp_path: "Path") -> None:
        """Raises KeyError when user_prompt_template is missing."""
        path = tmp_path / "bad.yaml"
        path.write_text(yaml.dump({"system_prompt": "test"}))
        with pytest.raises(KeyError, match="user_prompt_template"):
            load_prompt(path)


class TestFormatUserPrompt:
    def test_format_injects_context(self) -> None:
        """Replaces {context} placeholder with actual context."""
        template = "Materials:\n{context}\nDone."
        result = format_user_prompt(template, "video transcript here")
        assert "video transcript here" in result
        assert "{context}" not in result

    def test_format_preserves_template_text(self) -> None:
        """Non-placeholder text is preserved."""
        template = "Analyze:\n{context}\nReturn JSON."
        result = format_user_prompt(template, "data")
        assert result.startswith("Analyze:")
        assert result.endswith("Return JSON.\n")

    def test_format_empty_context(self) -> None:
        """Works with empty context string."""
        template = "Context: {context}"
        result = format_user_prompt(template, "")
        assert result == "Context: "


class TestPromptFileContent:
    def test_v1_prompt_loads_successfully(self) -> None:
        """The actual v1.yaml prompt file loads without errors."""
        data = load_prompt("prompts/architect/v1.yaml")
        assert "system_prompt" in data
        assert "user_prompt_template" in data
        assert "{context}" in data["user_prompt_template"]
```

---

## Структура файлів

```
prompts/architect/
└── v1.yaml                      # UPDATE: replace stub with real prompt

src/course_supporter/agents/
├── __init__.py                  # UPDATE: add exports
└── prompt_loader.py             # NEW: load_prompt, format_user_prompt

tests/unit/
└── test_prompt_loader.py        # NEW: ~8 tests
```

---

## Кроки виконання

1. Замінити `prompts/architect/v1.yaml` — повний system prompt + user template
2. Створити `agents/prompt_loader.py` — `load_prompt()`, `format_user_prompt()`
3. Оновити `agents/__init__.py` — exports
4. Створити `tests/unit/test_prompt_loader.py`
5. `make check`

---

## Примітки

- **YAML формат**: `yaml.safe_load()` — безпечний парсинг. Промпт зберігається як multi-line string (YAML `|` block scalar).
- **Версіонування**: поле `version` у YAML дозволяє відрізняти промпти. Для A/B тестування — `v2.yaml`, `v3.yaml` і т.д.
- **{context} placeholder**: `str.format()` — простий і достатній. Якщо потрібні складніші шаблони (Jinja2) — рефакторинг пізніше.
- **Dependency**: `pyyaml` вже є в `pyproject.toml` (transitive через structlog/pydantic-settings).
- **Prompt engineering**: промпт v1 — це starting point. Якість буде ітеративно покращуватись на основі eval results (Epic 6).
