"""DeepSeek provider — OpenAI-compatible client with thinking-mode override.

DeepSeek V4 (Flash / Pro) exposes a dual-mode API: thinking-on (default,
returns ``reasoning_content`` and inflates output/latency by an order
of magnitude) and thinking-off (non-think, ``content``-only, fast). For
the Pass 2a structured JSON workload (vision §2.2, KD-2.1-O) we want
non-think — the task is schema-bound mapping, not chain-of-thought
reasoning.

DeepSeek's OpenAI-compatible REST surface accepts the toggle as a
top-level body field; the OpenAI SDK exposes it via ``extra_body``.
This subclass forces ``extra_body={"thinking": {"type": "disabled"}}``
on every call, regardless of which stage routes through it. If a
future stage needs thinking-on (heavy reasoning), it should not route
through this provider — a separate ``DeepSeekThinkingProvider`` (or
per-stage override) would be the right surface.

Spike validation (2026-05-12):

* Thinking ON (default):  mean latency 140 s, output ~11 k tokens,
  cost ~$0.004 / call.
* Thinking OFF:           mean latency  19 s, output  ~1.8 k tokens,
  cost ~$0.0013 / call.
"""

from __future__ import annotations

from typing import Any

from course_supporter.llm.providers.openai_compat import OpenAICompatProvider


class DeepSeekProvider(OpenAICompatProvider):
    """OpenAI-compatible provider with DeepSeek thinking mode forced off."""

    def _extra_create_kwargs(self) -> dict[str, Any]:
        """Inject ``extra_body={"thinking": {"type": "disabled"}}`` (KD-2.1-O).

        Returned dict is spread into both
        :meth:`openai.AsyncOpenAI.chat.completions.create` (via
        :meth:`complete`) and instructor's
        ``create_with_completion`` (via :meth:`complete_structured`),
        so non-think mode applies to both unstructured and structured
        output paths.
        """
        return {"extra_body": {"thinking": {"type": "disabled"}}}
