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
    # Provider-independent reasoning-mode form (e.g. ``{"exclude": True}`` to
    # suppress thinking on Qwen3-VL). The DashScope connector translates it into
    # the vendor's native kwarg — ``enable_thinking=False`` (P5); a form its
    # connector cannot translate is refused at startup by the ladder validator
    # (P6, ``validate_ladders_against_registry``), never silently ignored on the
    # wire. Other providers do not read this field.
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
    # Reasoning tokens billed as a subset of ``tokens_out``. ``None`` = the
    # provider did not report them, ``0`` = reported zero. Only the DashScope
    # connector extracts this today (STEP-0 P5/P6); other providers leave it
    # ``None``.
    tokens_reasoning: int | None = None
    latency_ms: int = 0
    cost_usd: float | None = None
    action: str = ""
    strategy: str = "default"
    finished_at: datetime = Field(default_factory=datetime.now)
