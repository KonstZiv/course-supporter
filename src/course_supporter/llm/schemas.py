"""Shared schemas for LLM infrastructure."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class LLMRequest(BaseModel):
    """Input for LLM call."""

    prompt: str
    system_prompt: str | None = None
    model: str = ""  # set by StageRouter; providers fall back to default_model
    temperature: float = 0.0
    max_tokens: int | None = None
    action: str = ""  # video_analysis, course_structuring, ...
    strategy: str = "default"  # default, quality, budget
    contents: list[Any] | None = None  # multimodal: [url, text, Part, ...]
    # Vendor-specific reasoning-mode kwargs (e.g. ``{"exclude": True}`` for
    # Qwen3-VL via DashScope). Providers that recognise the field propagate
    # it verbatim; others ignore it.
    reasoning: dict[str, Any] | None = None
    # Caller declares the response must be JSON. Providers honour it by
    # returning bare JSON: native JSON mode where available (Gemini
    # ``response_mime_type``), and/or stripping markdown fences. Default
    # ``False`` leaves plain-text stages (e.g. Pass 2c denoise) untouched.
    expects_json: bool = False


class LLMResponse(BaseModel):
    """Unified response from any LLM provider."""

    content: str
    provider: str  # gemini, anthropic, openai, deepseek
    model_id: str  # gemini-2.5-flash, claude-sonnet-4, ...
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int = 0
    cost_usd: float | None = None
    action: str = ""
    strategy: str = "default"
    finished_at: datetime = Field(default_factory=datetime.now)
