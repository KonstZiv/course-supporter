"""What a ``deterministic: true`` stage sends to its model (2026-11-13).

Purpose:
    A stage that declares ``deterministic: true`` (``StageConfig.deterministic``;
    the path stages of ``config/submission_paths.yaml``) asks the same question
    the same way every time: temperature 0 is set on the request explicitly,
    and a fixed seed goes to every connector whose vendor documents one. The
    router applies both (``llm/stage_router.py::_build_request``); a stage with
    ``deterministic: false`` sends exactly what it sent before the flag was read.

Interface:
    :data:`REPRODUCIBILITY_SEED` and :data:`DETERMINISTIC_TEMPERATURE` — the two
    values, held here and nowhere else. Which connector puts the seed on the
    wire, and under which name, is the connector's own knowledge
    (:meth:`LLMProvider.seed_supported`), next to the documentation it rests on.

Limits:
    A seed narrows the spread of answers; no vendor read on 2026-11-13 promises
    identical output (DashScope: "the model returns the same result as much as
    possible"). The spread that remains is what ``scripts/measure_determinism.py``
    measures. The answer of the fallback rung ``deepseek_thinking`` is not
    reproducible: in DeepSeek's thinking mode neither a seed nor temperature
    acts.

Changing:
    Any value in ``[0, 2**31 - 1]`` serves every connector that takes a seed
    today (DashScope's documented range; Mistral states none). Changing it
    changes every answer of a deterministic stage, and every ``input_hash`` of
    its attempts (``llm/input_hash.py``) — so answers before and after the
    change are not comparable as "the same input".
"""

from __future__ import annotations

from typing import Final

# The day the product owner ratified it (2026-11-13), written as a number. The
# value carries no meaning beyond being fixed: what matters is that every
# attempt of a deterministic stage sends the same one.
REPRODUCIBILITY_SEED: Final = 20261113

# Set on every request of a deterministic stage rather than inherited from the
# request's default, so a change of that default cannot reach these stages.
# Note: DeepSeek's thinking mode (the ``deepseek_thinking`` connector) ignores
# temperature — "Thinking mode does not support the `temperature` ...
# parameters" (api-docs.deepseek.com/guides/thinking_mode, read 2026-11-13).
DETERMINISTIC_TEMPERATURE: Final = 0.0
