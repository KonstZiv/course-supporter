"""DeepSeek thinking-on provider — OpenAI-compatible without the disable hook.

Sibling of :class:`DeepSeekProvider`. Same base_url, same SDK, same key pool
(``DEEPSEEK_API_KEY``), BUT no ``_extra_create_kwargs`` override — the
base-class default returns ``{}``, so the call body omits the ``thinking``
field and the DeepSeek API falls back to its default thinking-on behaviour.

Per KD-2.4-T (Phase 2.4 task-2.4.17): thinking-on is opt-in for
reasoning-tier stages (video Pass 2a) where the full chain-of-thought
empirically holds segmentation balance on long contexts (5-way spike
2026-06-05 — DeepSeek V4 Pro with thinking on emitted 21k reasoning
tokens and balanced 7 segments with largest 23.5% of duration; the same
model with thinking forced off via DeepSeekProvider collapsed into a
67% catch-all tail). DeepSeekProvider (KD-2.4-S) stays the default for
schema-bound workloads (safety_check, Pass 2c denoise) where the
deterministic non-think mode is desirable.
"""

from __future__ import annotations

from course_supporter.llm.providers.openai_compat import OpenAICompatProvider


class DeepSeekThinkingProvider(OpenAICompatProvider):
    """OpenAI-compatible provider that lets DeepSeek thinking-mode default on.

    No ``_extra_create_kwargs`` override — the base-class neutral ``{}`` hook
    is exactly what we want: the request body omits ``thinking`` and the
    DeepSeek API applies its V4 default (thinking on, ``reasoning_content``
    surfaced alongside ``content``).
    """
