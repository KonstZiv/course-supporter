"""Shared schemas for LLM infrastructure."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator

from course_supporter.llm.finish_reason import FinishReason


class SchemaMode(StrEnum):
    """How a connector puts a response schema on the wire (task 09a).

    Chosen by the router from the rung model's registry capabilities, never by
    the stage: the stage only hands over the schema.

    * ``STRICT`` — the schema itself goes on the wire in the provider's form;
      the rung's model declares ``schema_strict``.
    * ``JSON`` — the provider's JSON mode without the schema (``json_object`` /
      ``response_mime_type``); the model declares ``json_mode`` only.
    """

    STRICT = "strict"
    JSON = "json"


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
    # Task 09a. The JSON Schema the stage's answer must follow, built by the
    # stage from its pydantic model (``strict_json_schema``). ``schema_mode``
    # says how the connector enforces it; the router sets it from the rung
    # model's capabilities, and ``None`` with a schema means "the model can
    # do neither" -- the wire is then exactly what it is without a schema. The
    # stage's own validator runs in every case: a schema holds the form of
    # the answer, not its content.
    response_schema: dict[str, Any] | None = None
    schema_mode: SchemaMode | None = None

    @model_validator(mode="after")
    def _schema_needs_json(self) -> Self:
        # A schema describes a JSON answer, so it implies the JSON contract the
        # connectors already honour (fence stripping, Gemini mime type). Saying
        # one without the other is a caller bug, not a request to guess.
        if self.response_schema is not None and not self.expects_json:
            msg = "response_schema requires expects_json=True"
            raise ValueError(msg)
        if self.schema_mode is not None and self.response_schema is None:
            msg = "schema_mode requires a response_schema"
            raise ValueError(msg)
        return self


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
    # Normalised by the connector from the vendor's own field (every connector
    # reads it). ``UNKNOWN`` only when the provider reported nothing.
    finish_reason: FinishReason = FinishReason.UNKNOWN
    latency_ms: int = 0
    cost_usd: float | None = None
    action: str = ""
    strategy: str = "default"
    finished_at: datetime = Field(default_factory=datetime.now)
