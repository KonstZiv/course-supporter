"""DeepSeek provider — OpenAI-compatible client with thinking-mode override.

DeepSeek V4 (Flash / Pro) exposes a dual-mode API: thinking-on (default,
returns ``reasoning_content`` and inflates output/latency by an order
of magnitude) and thinking-off (non-think, ``content``-only, fast). For
schema-bound structured-JSON workloads (safety_check, Pass 2c denoise)
we want non-think — the task is schema-bound mapping, not chain-of-
thought reasoning. This contract is now ratified as KD-2.4-S (Phase
2.4, 2026-06-05); prior to that it lived as an orphan decision from
the Phase 2.1 C5 commit ``bf54b30`` and code-comments mis-attributed
it to KD-2.1-O — that mis-attribution is corrected here. KD-2.1-O
remains a separate concern (per-source-type Pass 2a structure,
content vs no-content).

DeepSeek's OpenAI-compatible REST surface accepts the toggle as a
top-level body field; the OpenAI SDK exposes it via ``extra_body``.
This subclass forces ``extra_body={"thinking": {"type": "disabled"}}``
on every call routed through it. Stages that need thinking-on (heavy
reasoning, e.g. video Pass 2a per KD-2.4-T) route through the sibling
:class:`DeepSeekThinkingProvider` instead — its base-class default
``{}`` hook lets the DeepSeek API apply its V4 thinking-on default.

Spike validation (2026-05-12, DeepSeek V4 Flash):

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
        """Inject ``extra_body={"thinking": {"type": "disabled"}}`` (KD-2.4-S).

        Returned dict is spread into both
        :meth:`openai.AsyncOpenAI.chat.completions.create` (via
        :meth:`complete`) and instructor's
        ``create_with_completion`` (via :meth:`complete_structured`),
        so non-think mode applies to both unstructured and structured
        output paths.
        """
        return {"extra_body": {"thinking": {"type": "disabled"}}}
