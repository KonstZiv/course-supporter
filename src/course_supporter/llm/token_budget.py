"""Input-size estimation for token-budget pre-flight checks.

Single definition of the formula for the KD16
:mod:`course_supporter.llm.stage_router`. KD10 «Token budget policy»
requires a deterministic estimate of input tokens before the LLM
call so the router can skip rungs whose context window will not
fit. The estimate is computed in code; the LLM is never asked.
"""

from __future__ import annotations

# Average chars per token across common LLM tokenizers.
# Conservative estimate (lower ratio = higher token count = safer guard).
_CHARS_PER_TOKEN = 3.5


def estimate_tokens(prompt: str, system_prompt: str | None = None) -> int:
    """Estimate token count from text length.

    Uses a conservative chars-per-token ratio (~3.5) to avoid
    underestimating. Accuracy is ±15-20%, sufficient for pre-flight
    context window guards.
    """
    total_chars = len(prompt)
    if system_prompt:
        total_chars += len(system_prompt)
    return int(total_chars / _CHARS_PER_TOKEN)
